# Adapted from HuggingFace Transformers Library
# https://github.com/huggingface/transformers/blob/17a55534f5e5df10ac4804d4270bf6b8cc24998d/src/transformers/models/llama/modeling_llama.py

import math
from typing import Tuple

import torch
import torch_int
from torch import nn
from transformers.models.llama.modeling_llama import (
    LlamaConfig,
    LlamaRMSNorm,
    PreTrainedModel,
    rotate_half,
)
from transformers.activations import ACT2FN

from punica.ops import append_kv, init_kv, mha_rope_decode, rms_norm, gemm_forward, gemv_forward
from punica.utils import BatchedKvCache, BatchLenInfo


def rotary_pos_emb(q, k, beg):
  device = q.device
  dtype = q.dtype
  bsz, nhead, seqlen, dim = q.shape
  end = beg + seqlen

  base = 10000
  inv_freq = 1.0 / (base**(torch.arange(0, dim, 2).float().to(device) / dim))
  t = torch.arange(beg, end, device=device, dtype=dtype)
  freqs = torch.einsum("i,j->ij", t, inv_freq)
  emb = torch.cat((freqs, freqs), dim=-1).unsqueeze(0).unsqueeze(0)
  cos = emb.cos()
  sin = emb.sin()
  q_embed = (q * cos) + (rotate_half(q) * sin)
  k_embed = (k * cos) + (rotate_half(k) * sin)
  return q_embed.to(q.dtype), k_embed.to(k.dtype)

class LinearInt4(nn.Module):

  def __init__(self, in_features, out_features, out_dtype, bias=False):
    super().__init__()
    assert bias is False
    self.in_features = in_features
    self.out_features = out_features
    self.out_dtype = out_dtype
    self.weight = nn.Parameter(
        torch.empty(
            out_features, in_features, dtype=torch.int8),
        requires_grad=False)
    if out_dtype == "int8":
      self.fake_bias = nn.Parameter(
          torch.empty(out_features, dtype=torch.int8), requires_grad=False)
    else:
      self.fake_bias = nn.Parameter(
          torch.empty(out_features, dtype=torch.float32), requires_grad=False)
    self.register_parameter("bias", None)

  def forward(self, input):
    f = {
        "int8": torch_int._CUDA.linear_a8_w8_b8_o8,
        "fp32": torch_int._CUDA.linear_a8_w8_bfp32_ofp32,
    }[self.out_dtype]
    # HACK: for throughput evaluation.
    return f(input, self.weight, self.fake_bias, 1.0, 1.0)

class LlamaMLP(nn.Module):
  def __init__(self, config):
    super().__init__()
    self.config = config
    self.hidden_size = config.hidden_size
    self.intermediate_size = config.intermediate_size
    self.gate_proj = LinearInt4(self.hidden_size, self.intermediate_size, out_dtype="fp32", bias=False)
    self.up_proj = LinearInt4(self.hidden_size, self.intermediate_size, out_dtype="fp32", bias=False)
    self.down_proj = LinearInt4(self.intermediate_size, self.hidden_size, out_dtype="fp32", bias=False)
    self.act_fn = ACT2FN[config.hidden_act]

  def forward(self, x):
    gate = self.gate_proj(x).to(dtype=torch.float16)
    up = self.up_proj(x).to(dtype=torch.float16)
    x = self.act_fn(gate) * up
    x = x.round().clamp(-128, 127).to(torch.int8)
    return self.down_proj(x).to(torch.float16)
  
class LlamaAttention(nn.Module):

  def __init__(self, config: LlamaConfig, layer_idx: int):
    super().__init__()
    self.config = config
    self.hidden_size = config.hidden_size
    self.num_heads = config.num_attention_heads
    self.head_dim = self.hidden_size // self.num_heads
    self._scale = 1 / math.sqrt(self.head_dim)
    self.layer_idx = layer_idx

    if (self.head_dim * self.num_heads) != self.hidden_size:
      raise ValueError(
          f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
          f" and `num_heads`: {self.num_heads}).")
    self.q_proj = LinearInt4(
        self.hidden_size, self.num_heads * self.head_dim, out_dtype="int8", bias=False)
    self.k_proj = LinearInt4(
        self.hidden_size, self.num_heads * self.head_dim, out_dtype="int8", bias=False)
    self.v_proj = LinearInt4(
        self.hidden_size, self.num_heads * self.head_dim, out_dtype="int8", bias=False)
    self.o_proj = LinearInt4(
        self.num_heads * self.head_dim, self.hidden_size, out_dtype="fp32", bias=False)

  def forward(
      self,
      hidden_states: torch.Tensor,
      blen: BatchLenInfo,
      prefill_kv: BatchedKvCache | None,
      decode_kv: BatchedKvCache | None,
  ) -> torch.Tensor:
    torch.cuda.nvtx.range_push("qkv_proj")
    q_proj = self.q_proj(hidden_states)
    k_proj = self.k_proj(hidden_states)
    v_proj = self.v_proj(hidden_states)
    torch.cuda.nvtx.range_pop()
    stack_attn_output = []

    if len(blen.prefills) > 0:
      torch.cuda.nvtx.range_push("init_kv")
      assert prefill_kv is not None
      init_kv(
          prefill_kv,
          k_proj[:blen.doff].view(-1, self.num_heads, self.head_dim),
          v_proj[:blen.doff].view(-1, self.num_heads, self.head_dim),
          blen.indptr,
          self.layer_idx,
      )
      torch.cuda.nvtx.range_pop()

      # q_projs = q_proj[:blen.doff].split(blen.prefills)
      # k_projs = k_proj[:blen.doff].split(blen.prefills)
      # v_projs = v_proj[:blen.doff].split(blen.prefills)
      for batch_idx, q_len in enumerate(blen.prefills):
        torch.cuda.nvtx.range_push(f"batch_idx={batch_idx}")
        torch.cuda.nvtx.range_push("transpose")
        # query_states = q_projs[batch_idx].view(1, q_len, self.num_heads,
        #                                        self.head_dim).transpose(1, 2)
        # key_states = k_projs[batch_idx].view(1, q_len, self.num_heads,
        #                                      self.head_dim).transpose(1, 2)
        # value_states = v_projs[batch_idx].view(1, q_len, self.num_heads,
        #                                        self.head_dim).transpose(1, 2)
        # (1, n, s, d)
        query_states = torch.randn(1, q_len, self.num_heads, self.head_dim, device=q_proj.device, dtype=torch.float16).transpose(1, 2)
        key_states = torch.randn(1, q_len, self.num_heads, self.head_dim, device=k_proj.device, dtype=torch.float16).transpose(1, 2)
        value_states = torch.randn(1, q_len, self.num_heads, self.head_dim, device=v_proj.device, dtype=torch.float16).transpose(1, 2)
        torch.cuda.nvtx.range_pop()

        torch.cuda.nvtx.range_push("pos_emb")
        query_states, key_states = rotary_pos_emb(query_states, key_states, 0)
        torch.cuda.nvtx.range_pop()

        query_states = query_states.squeeze(0)
        key_states = key_states.squeeze(0)
        value_states = value_states.squeeze(0)
        # (n, s, d)

        # scaled dot product attention
        torch.cuda.nvtx.range_push("sdpa")
        attn_output = torch.nn.functional.scaled_dot_product_attention(
            query_states, key_states, value_states, is_causal=True)
        attn_output = attn_output.transpose(0, 1)
        attn_output = attn_output.reshape(q_len, self.hidden_size)
        attn_output = attn_output.round().clamp(-128, 127).to(torch.int8)
        stack_attn_output.append(attn_output)
        torch.cuda.nvtx.range_pop()
        torch.cuda.nvtx.range_pop()

    if blen.decode > 0:
      q = q_proj[blen.doff:].view(blen.decode, self.num_heads, self.head_dim)
      k = k_proj[blen.doff:].view(blen.decode, self.num_heads, self.head_dim)
      v = v_proj[blen.doff:].view(blen.decode, self.num_heads, self.head_dim)

      torch.cuda.nvtx.range_push("append_kv")
      assert decode_kv is not None
      append_kv(decode_kv, k, v, self.layer_idx)
      torch.cuda.nvtx.range_pop()

      torch.cuda.nvtx.range_push(f"batch_decode")
      attn_outputs = mha_rope_decode(q, decode_kv, self.layer_idx)
      attn_outputs = attn_outputs.view(blen.decode, self.hidden_size)
      stack_attn_output.append(attn_outputs)
      torch.cuda.nvtx.range_pop()

    if len(stack_attn_output) == 1:
      attn_outputs = stack_attn_output[0]
    else:
      attn_outputs = torch.cat(stack_attn_output, dim=0)

    # output projection
    torch.cuda.nvtx.range_push("o_proj")
    attn_output = self.o_proj(attn_outputs)
    torch.cuda.nvtx.range_pop()

    return attn_output.to(torch.float16)


class RMSNormQ(nn.Module):

  def __init__(self, hidden_size, eps=1e-6):
    super().__init__()
    self.weight = nn.Parameter(torch.ones(hidden_size))
    self.variance_epsilon = eps

  def forward(self, hidden_states):
    assert hidden_states.dtype == torch.float16
    v = rms_norm(hidden_states, self.weight, self.variance_epsilon)
    v = v.round().clamp(-128, 127).to(torch.int8)
    return v


class LlamaDecoderLayer(nn.Module):

  def __init__(self, config: LlamaConfig, layer_idx: int):
    super().__init__()
    self.hidden_size = config.hidden_size
    self.self_attn = LlamaAttention(config=config, layer_idx=layer_idx)
    self.mlp = LlamaMLP(config)
    self.input_layernorm = RMSNormQ(
        config.hidden_size, eps=config.rms_norm_eps)
    self.post_attention_layernorm = RMSNormQ(
        config.hidden_size, eps=config.rms_norm_eps)

  def forward(
      self,
      hidden_states: torch.Tensor,
      blen: BatchLenInfo,
      prefill_kv: BatchedKvCache | None,
      decode_kv: BatchedKvCache | None,
  ) -> torch.Tensor:
    residual = hidden_states

    torch.cuda.nvtx.range_push("input_norm")
    hidden_states = self.input_layernorm(hidden_states)
    torch.cuda.nvtx.range_pop()

    # Self Attention
    torch.cuda.nvtx.range_push("LlamaAttention")
    hidden_states = self.self_attn(hidden_states, blen, prefill_kv, decode_kv)
    torch.cuda.nvtx.range_pop()
    torch.cuda.nvtx.range_push("r")
    hidden_states = residual + hidden_states
    torch.cuda.nvtx.range_pop()
    
    # Fully Connected
    residual = hidden_states
    torch.cuda.nvtx.range_push("norm")
    hidden_states = self.post_attention_layernorm(hidden_states)
    torch.cuda.nvtx.range_pop()
    torch.cuda.nvtx.range_push("mlp")
    hidden_states = self.mlp(hidden_states)
    torch.cuda.nvtx.range_pop()
    torch.cuda.nvtx.range_push("r")
    hidden_states = residual + hidden_states
    torch.cuda.nvtx.range_pop()

    return hidden_states


class LlamaPreTrainedModel(PreTrainedModel):
  config_class = LlamaConfig
  base_model_prefix = "model"
  supports_gradient_checkpointing = False
  _no_split_modules = ["LlamaDecoderLayer"]
  _keys_to_ignore_on_load_unexpected = [
      r"decoder\.version",
      r"self_attn\.rotary_emb\.inv_freq",
  ]


class LlamaModel(LlamaPreTrainedModel):

  def __init__(self, config: LlamaConfig):
    super().__init__(config)
    self.padding_idx = config.pad_token_id
    self.vocab_size = config.vocab_size
    self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size,
                                     self.padding_idx)
    self.layers = nn.ModuleList(
        # [LlamaDecoderLayer(config, i) for i in range(config.num_hidden_layers)])
        [LlamaDecoderLayer(config, 0) for _ in range(config.num_hidden_layers)]) # Hack for memory usage
    self.norm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
    self.post_init()

  def forward(
      self,
      input_ids: torch.Tensor,
      blen: BatchLenInfo,
      prefill_kv: BatchedKvCache | None,
      decode_kv: BatchedKvCache | None,
  ) -> torch.Tensor:
    torch.cuda.nvtx.range_push(f"embed")
    hidden_states = self.embed_tokens(input_ids)
    torch.cuda.nvtx.range_pop()

    # import pdb; pdb.set_trace()
    
    for layer_idx, decoder_layer in enumerate(self.layers):
      torch.cuda.nvtx.range_push(f"layer={layer_idx}")
      hidden_states = decoder_layer(hidden_states, blen, prefill_kv, decode_kv)
      # print(f"layer={layer_idx}, hidden_states={hidden_states}")
      torch.cuda.nvtx.range_pop()

    torch.cuda.nvtx.range_push("lastnorm")
    hidden_states = self.norm(hidden_states)
    torch.cuda.nvtx.range_pop()

    return hidden_states


class LlamaForCausalLM(LlamaPreTrainedModel):

  def __init__(self, config):
    super().__init__(config)
    self.model = LlamaModel(config)
    self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
    self.post_init()

  def forward(
      self,
      input_ids: torch.Tensor,
      blen: BatchLenInfo,
      prefill_kv: BatchedKvCache | None,
      decode_kv: BatchedKvCache | None,
  ) -> Tuple[torch.Tensor, torch.Tensor]:
    torch.cuda.nvtx.range_push("LlamaForCausalLM")
    hidden_states = self.model(input_ids, blen, prefill_kv, decode_kv)
    torch.cuda.nvtx.range_push("lm_head")
    logits = self.lm_head(hidden_states)
    torch.cuda.nvtx.range_pop()
    torch.cuda.nvtx.range_pop()
    return logits, hidden_states
