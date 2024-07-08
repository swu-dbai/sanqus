import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from dist_utils import DistEnv
import torch.distributed as dist

try:
    from spmm_cpp import spmm_cusparse_coo, spmm_cusparse_csr
    def spmm(A,B,C): 
        if DistEnv.env.csr_enabled:
            spmm_cusparse_csr(A.crow_indices().int(), A.col_indices().int(), A.values(), A.size(0), A.size(1), \
                B, C, 1.0, 1.0, DistEnv.env.half_enabled)
        else:
            spmm_cusparse_coo(A.indices()[0].int(), A.indices()[1].int(), A.values(), A.size(0), A.size(1), \
                B, C, 1.0, 1.0, DistEnv.env.half_enabled)
except ImportError as e:
    print('no spmm cpp:', e)
    spmm = lambda A,B,C: C.addmm_(A,B)


def compute_minmax_params(input):
    rmin, rmax = torch.min(input, dim=1)[0], torch.max(input, dim=1)[0]
    return rmin, rmax


def quantization(features, nbits=8, is_stochastic=True):
    rmin, rmax = compute_minmax_params(features)
    rscale = (2**nbits - 1) / (rmax - rmin)
    q_features = (features - rmin.unsqueeze(1)) * rscale.unsqueeze(1)
    if is_stochastic:
        q_features = torch.clamp(torch.round(q_features + torch.rand_like(q_features) - 0.5), 0, 2**nbits - 1)
    else:
        q_features = torch.clamp(torch.round(q_features), 0, 2**nbits - 1)
    if nbits != 8:
        raise RuntimeError("Shitty Pytorch :)")
    return q_features.type(torch.uint8), rscale, rmin


def dequantization(q_features, rscale, rmin):
    return q_features / rscale.unsqueeze(1) + rmin.unsqueeze(1)


from collections import defaultdict
g_cache = defaultdict(dict)
g_cache_enabled = {'ForwardL1': True, 'ForwardL2': True,
                   'BackwardL1': False, 'BackwardL2': False }
g_cache_enabled = {'ForwardL1': False, 'ForwardL2': False,
                   'BackwardL1': False, 'BackwardL2': False }

g_bcast_counter = defaultdict(lambda: defaultdict(int))
g_epoch_counter = defaultdict(int)

def use_cache(tag, src):
    F_L1 = tag == 'ForwardL1' and g_bcast_counter[tag][src]>0 # if there is enough gpu mem
    F_L2 = tag == 'ForwardL2' and (g_bcast_counter[tag][src]>50 and g_epoch_counter[tag]%2==0)
    use = g_cache_enabled[tag] and (F_L1 or F_L2)
    if use:
        assert(src in g_cache[tag])
    return use


def cached_broadcast(local_adj_parts, local_feature, tag):
    env = DistEnv.env
    z_loc = torch.zeros_like(local_feature)
    feature_bcast = torch.zeros_like(local_feature)
    # print('bcast feature', feature_bcast)
    g_epoch_counter[tag] += 1
    
    for src in range(env.world_size):
        if src==env.rank:
            feature_bcast = local_feature.clone()
        # env.barrier_all()
        with env.timer.timing_cuda('broadcast'):
            if not use_cache(tag, src):
                with env.timer.timing_cuda(f'broadcast {tag} {src}'):
                    dist.broadcast(feature_bcast, src=src)
                    g_bcast_counter[tag][src] += 1
                    if g_cache_enabled[tag]:
                        g_cache[tag][src] = feature_bcast.clone()
                # env.logger.log('not cached', tag, src, 'counter', g_bcast_counter[tag][src])
            else:
                # env.logger.log('cached', tag, src)
                feature_bcast = g_cache[tag][src]
        with env.timer.timing_cuda('spmm'):
            spmm(local_adj_parts[src], feature_bcast, z_loc)
    return z_loc


def cached_broadcast_fwd(local_adj_parts, local_feature, tag):
    env = DistEnv.env
    z_loc = torch.zeros_like(local_feature, device=local_feature.device)
    dq = torch.zeros_like(local_feature, device=local_feature.device)
    # print("before quantization:", dq.numel()*dq.element_size())
    q_feature_bcast = torch.zeros_like(local_feature, device=local_feature.device).type(torch.uint8)
    rscale = torch.zeros(local_feature.size()[0], device=local_feature.device)
    rmin = torch.zeros(local_feature.size()[0], device=local_feature.device)

    g_epoch_counter[tag] += 1

    # print("after quantization:", q_feature_bcast.numel()*q_feature_bcast.element_size())
    for src in range(env.world_size):
        if src==env.rank:
            q_feature_bcast, rscale, rmin = quantization(local_feature)
        # env.barrier_all()
        with env.timer.timing_cuda('broadcast'):
            if not use_cache(tag, src):
                with env.timer.timing_cuda(f'broadcast {tag} {src}'):
                    dist.broadcast(q_feature_bcast, src=src)
                    dist.broadcast(rscale, src=src)
                    dist.broadcast(rmin, src=src)
                    
                    g_bcast_counter[tag][src] += 1
                    if g_cache_enabled[tag]:
                        g_cache[tag][src] = (q_feature_bcast.clone(), rscale.clone(), rmin.clone())
                # env.logger.log('not cached', tag, src, 'counter', g_bcast_counter[tag][src])
            else:
                # env.logger.log('cached', tag, src)
                q_feature_bcast, rscale, rmin = g_cache[tag][src]
        with env.timer.timing_cuda('quantization'):
            dq = dequantization(q_feature_bcast, rscale, rmin)
        with env.timer.timing_cuda('spmm'):
            spmm(local_adj_parts[src], dq, z_loc)
    return z_loc


class DistGCNLayer(torch.autograd.Function):
    @staticmethod
    def forward(ctx, features, weight, adj_parts, tag):
        ctx.save_for_backward(features, weight)
        ctx.adj_parts = adj_parts
        ctx.tag = tag
        z_local = cached_broadcast_fwd(adj_parts, features, 'Forward'+tag)
        with DistEnv.env.timer.timing_cuda('mm'):
            z_local = torch.mm(z_local, weight)
        return z_local

    @staticmethod
    def backward(ctx, grad_output):
        features,  weight = ctx.saved_tensors
        ag = cached_broadcast(ctx.adj_parts, grad_output, 'Backward'+ctx.tag)
        with DistEnv.env.timer.timing_cuda('mm'):
            grad_features = torch.mm(ag.to(dtype=torch.float), weight.t())
            grad_weight = torch.mm(features.t(), ag)
        with DistEnv.env.timer.timing_cuda('all_reduce'):
            DistEnv.env.all_reduce_sum(grad_weight)
        return grad_features, grad_weight, None, None


class QCachedGCN(nn.Module):
    def __init__(self, g, env, hidden_dim=16):
        super().__init__()
        self.g, self.env = g, env
        in_dim, out_dim = g.features.size(1), g.num_classes
        torch.manual_seed(0)
        self.weight1 = nn.Parameter(torch.rand(in_dim, hidden_dim).to(env.device))
        self.weight2 = nn.Parameter(torch.rand(hidden_dim, hidden_dim).to(env.device))
        self.weight3 = nn.Parameter(torch.rand(hidden_dim, out_dim).to(env.device))
        # for weight in [self.weight1, self.weight2, self.weight3]:
            # nn.init.xavier_uniform_(weight)

    def forward(self, features):
        hidden_features = F.relu(DistGCNLayer.apply(features, self.weight1, self.g.adj_parts, 'L1'))
        # hidden_features = F.relu(DistGCNLayer.apply(hidden_features, self.weight2, self.g.adj_parts, 'L2'))
        outputs = DistGCNLayer.apply(hidden_features, self.weight3, self.g.adj_parts,  'L2')
        return outputs
        # return F.log_softmax(outputs, 1)

