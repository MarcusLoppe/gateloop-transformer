import torch
from torch.nn import Module, ModuleList
from torch import nn, einsum, Tensor
import torch.nn.functional as F

from einops import rearrange
from einops.layers.torch import Rearrange

from rotary_embedding_torch import RotaryEmbedding

from gateloop_transformer.associative_scan import associative_scan

# helpers

def exists(v):
    return v is not None

def default(v, d):
    return v if exists(v) else d

# rms norm

class RMSNorm(Module):
    def __init__(self, dim):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return F.normalize(x, dim = -1) * self.scale * self.gamma

# feedforward

def FeedForward(dim, mult = 4):
    dim_inner = dim * mult
    return nn.Sequential(
        RMSNorm(dim),
        nn.Linear(dim, dim_inner),
        nn.GELU(),
        nn.Linear(dim_inner, dim)
    )

# attention

class CausalFullAttention(Module):
    def __init__(
        self,
        dim,
        *,
        dim_head = 64,
        heads = 8,
        data_dependent_rel_pos = False,
        frac_gradient_data_dependent_rel_pos = 0.5,
        softmax_normalize = None
    ):
        super().__init__()
        dim_inner = dim_head * heads
        self.softmax_normalize = default(softmax_normalize, not data_dependent_rel_pos)

        self.scale = dim_head ** -0.5

        self.norm = RMSNorm(dim)

        self.to_qkv = nn.Sequential(
            nn.Linear(dim, dim_inner * 3),
            Rearrange('b n (qkv h d) -> qkv b h n d', h = heads, qkv = 3)
        )

        self.data_dependent_rel_pos = data_dependent_rel_pos
        self.frac_gradient_data_dependent_rel_pos = frac_gradient_data_dependent_rel_pos

        if data_dependent_rel_pos:
            self.to_a = nn.Sequential(
                nn.Linear(dim, dim_inner * 2),
                Rearrange('b n (h d c) -> b h n d c', h = heads, c = 2)
            )

        self.to_out = nn.Sequential(
            Rearrange('b h n d -> b n (h d)'),
            nn.Linear(dim_inner, dim)
        )

    def forward(self, x, ablate_complex = False):
        x = self.norm(x)

        q, k, v = self.to_qkv(x)

        q = q * self.scale

        if self.data_dependent_rel_pos:
            frac_gradient = self.frac_gradient_data_dependent_rel_pos

            a = self.to_a(x)

            # allow for data dependent relative position projection to change more slowly
            # alternative to using a lowered learning rate mentioned in paper

            a = a * frac_gradient + a.detach() * (1 - frac_gradient)

            a = torch.view_as_complex(a)

            if ablate_complex:
                a = a.real + 0.j

            magnitude, phase = a.abs(), a.angle()
            a = torch.polar(magnitude.sigmoid(), phase)

            a_cumprod = a.cumprod(dim = -2)

            a_cumprod_real = a_cumprod.real
            a_cumprod_real_inverse = 1. / a_cumprod_real.clamp(min = 1e-10)

            q = q * a_cumprod_real
            k = k * a_cumprod_real_inverse

        sim = einsum('b h i d, b h j d -> b h i j', q, k)

        i, j = sim.shape[2:]
        causal_mask = torch.ones((i, j), dtype = torch.bool, device = x.device).triu(j - i + 1)

        if self.softmax_normalize:
            sim = sim.masked_fill(causal_mask, -torch.finfo(sim.dtype).max)
            attn = sim.softmax(dim = -1)
        else:
            attn = sim.masked_fill(causal_mask, 0.)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)
        return self.to_out(out)

# data gated linear attention with "gateloop operator"

def gate_loop_operator(q, k, v, a):
    """
    the pseudocode in section 3.2 of the paper
    """

    kv = einsum('b n d, b n e -> b n d e', k, v)

    def binary_operator(a, b):
        a_i, kv_i = a
        a_j, kv_j = b

        return a_j * a_i, a_j.real * kv_i + kv_j

    a = rearrange(a, '... -> ... 1')

    # activations for state transitions
    # sigmoid for magnitude, identity for phase

    magnitude, phase = a.abs(), a.angle()
    a = torch.polar(magnitude.sigmoid(), phase)

    _, kv = associative_scan(binary_operator, (a, kv))

    return einsum('b n d, b n d e -> b n e', q, kv)

class GateLoopedAttention(Module):
    def __init__(
        self,
        dim,
        dim_inner = None,
        frac_gradient_state_transition = 0.5
    ):
        super().__init__()
        self.frac_gradient_state_transition = frac_gradient_state_transition

        dim_inner = default(dim_inner, dim)

        self.norm = RMSNorm(dim)

        self.to_qkv = nn.Linear(dim, dim_inner * 3, bias = False)

        self.to_a = nn.Sequential(
            nn.Linear(dim, dim_inner * 2),
            Rearrange('... (d c) -> ... d c', c = 2)
        )

        self.to_out = nn.Linear(dim_inner, dim, bias = False) if dim_inner != dim else nn.Identity()

    def forward(self, x, ablate_complex = False):
        frac_gradient = self.frac_gradient_state_transition

        x = self.norm(x)

        q, k, v = self.to_qkv(x).chunk(3, dim = -1)

        a = self.to_a(x)
        a = a * frac_gradient + a.detach() * (1 - frac_gradient)

        a = torch.view_as_complex(a)

        if ablate_complex:
            a = a.real + 0.j

        out = gate_loop_operator(q, k, v, a)

        return self.to_out(out)

# main class

class Transformer(Module):
    def __init__(
        self,
        dim,
        *,
        num_tokens,
        depth,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        use_gate_looped_attn = True,
        dim_gate_looped_attn = None,
        attn_softmax_normalize = None,
        data_dependent_rel_pos = False,
        frac_gradient_state_transition = 0.5,
        ablate_complex = False
    ):
        super().__init__()
        self.ablate_complex = ablate_complex

        self.token_emb = nn.Embedding(num_tokens, dim)

        layers = ModuleList([])

        for _ in range(depth):

            if use_gate_looped_attn:
                spatial_mixer = GateLoopedAttention(
                    dim = dim,
                    dim_inner = dim_gate_looped_attn,
                    frac_gradient_state_transition = frac_gradient_state_transition
                )
            else:
                spatial_mixer = CausalFullAttention(
                    dim = dim,
                    dim_head = dim_head,
                    heads = heads,
                    softmax_normalize = attn_softmax_normalize,
                    data_dependent_rel_pos = data_dependent_rel_pos,
                    frac_gradient_data_dependent_rel_pos = frac_gradient_state_transition
                )

            layers.append(ModuleList([
                spatial_mixer,
                FeedForward(
                    dim = dim,
                    mult = ff_mult
                )
            ]))

        self.layers = ModuleList(layers)

        self.to_logits = nn.Sequential(
            RMSNorm(dim),
            nn.Linear(dim, num_tokens, bias = False)
        )

    def forward(
        self,
        x,
        return_loss = False,
        ablate_complex = None
    ):
        ablate_complex = default(ablate_complex, self.ablate_complex)

        if return_loss:
            x, labels = x[:, :-1], x[:, 1:]

        x = self.token_emb(x)

        for attn, ff in self.layers:
            x = attn(x, ablate_complex = ablate_complex) + x
            x = ff(x) + x

        logits = self.to_logits(x)

        if not return_loss:
            return logits

        logits = rearrange(logits, 'b n c -> b c n')
        return F.cross_entropy(logits, labels)
