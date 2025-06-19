from typing import List
import os
import json
import functools
import torch

from maga_transformer.config.gpt_init_model_parameters import GptInitModelParameters
from maga_transformer.utils.model_weight import W, CkptWeightInfo, \
    identity, sp_0, sp_head_lora, sp_id, sp_neg1, zeros, transpose, concat_1, concat_0, merge_qkv_lora_A, merge_qkv_lora_B, stack_, stack_moe_w1
from maga_transformer.models.base_model import BaseModel
from maga_transformer.model_factory_register import register_model
from maga_transformer.model_loader.model_weight_info import ModelWeightInfo, ModelDeployWeightInfo
from maga_transformer.model_loader.weight_module import AtomicWeight, WeightModule
from maga_transformer.model_loader.attn_weight import AttnAtomicWeight, AttnConfig
from maga_transformer.model_loader.ffn_weight import MoeAtomicWeight, MoeConfig, MoeWeight

def merge_qkv_hf(ts: List[torch.Tensor]):
    q, k, v = ts
    qkv_weight = torch.concat([q.T, k.T, v.T], dim=1).contiguous()
    return qkv_weight

class MixtralWeightInfo(ModelDeployWeightInfo):
    @property
    def support_lora(self):
        return True

    def _get_weight_info(self):
        attn_config = AttnConfig(head_num=self._head_num,
                                  head_num_kv=self._head_num_kv,
                                  hidden_size=self._hidden_size,
                                  size_per_head=self._size_per_head)
        moe_config = MoeConfig(expert_num=self.expert_num_,
                              inter_padding_size=self._inter_padding_size,
                              routed_scaling_factor=1.0)

        weights = [
            AtomicWeight(W.embedding, [CkptWeightInfo('model.embed_tokens.weight', concat_1)], identity),
            AtomicWeight(W.lm_head, [CkptWeightInfo('lm_head.weight', identity)], identity),
            AtomicWeight(W.final_ln_gamma, [CkptWeightInfo('model.norm.weight', identity)], identity),
            AtomicWeight(W.final_ln_beta, [], functools.partial(zeros, shape=[self._hidden_size])),
        ]
        layer_weights: List[WeightModule] = [
            AtomicWeight(W.pre_ln_gamma, [CkptWeightInfo('model.layers.{i}.input_layernorm.weight', identity)], identity),
            AttnAtomicWeight(W.attn_o_w, [CkptWeightInfo('model.layers.{i}.self_attn.o_proj.weight', concat_1)], transpose,
                         config=attn_config, lora_a_process_func=transpose, lora_b_process_func=transpose,
                         lora_a_split_func=sp_0, lora_b_split_func=sp_id),
            AtomicWeight(W.post_ln_gamma, [CkptWeightInfo('model.layers.{i}.post_attention_layernorm.weight', identity)], identity),
        ]

        layer_weights.append(
            AttnAtomicWeight(W.attn_qkv_w,
                        [CkptWeightInfo('model.layers.{i}.self_attn.q_proj.weight', concat_0),
                        CkptWeightInfo('model.layers.{i}.self_attn.k_proj.weight', concat_0),
                        CkptWeightInfo('model.layers.{i}.self_attn.v_proj.weight', concat_0)],
                        functools.partial(merge_qkv_hf),
                        config=attn_config,
                        lora_a_process_func=functools.partial(merge_qkv_lora_A, allow_empty=False, hidden_size=self._hidden_size, head_num=self._head_num, head_num_kv=self._head_num_kv, size_per_head=self._size_per_head),
                        lora_b_process_func=functools.partial(merge_qkv_lora_B, allow_empty=False, hidden_size=self._hidden_size, head_num=self._head_num, head_num_kv=self._head_num_kv, size_per_head=self._size_per_head),
                        lora_a_split_func=sp_id,
                        lora_b_split_func=sp_head_lora
            )
        )

        ffn_w1: List[CkptWeightInfo] = []
        ffn_w2: List[CkptWeightInfo] = []
        ffn_w1.append(CkptWeightInfo('model.layers.{i}.block_sparse_moe.experts.{expert_id}.w3.weight', identity))
        ffn_w1.append(CkptWeightInfo('model.layers.{i}.block_sparse_moe.experts.{expert_id}.w1.weight', identity))
        ffn_w2.append(CkptWeightInfo('model.layers.{i}.block_sparse_moe.experts.{expert_id}.w2.weight', identity))
        layer_weights.append(
            MoeWeight(
                sub_weights=[
                    MoeAtomicWeight(W.moe_gate, [CkptWeightInfo('model.layers.{i}.block_sparse_moe.gate.weight', concat_0)], transpose,
                                    config=moe_config, 
                                    lora_a_process_func=transpose, lora_b_process_func=transpose,
                                    lora_a_split_func=sp_id, lora_b_split_func=sp_neg1),
                    MoeAtomicWeight(W.moe_w1, ffn_w1, stack_moe_w1, 
                                    config=moe_config, 
                                    lora_a_process_func=stack_moe_w1, lora_b_process_func=stack_moe_w1,
                                    lora_a_split_func=sp_id, lora_b_split_func=sp_neg1),
                    MoeAtomicWeight(W.moe_w2, ffn_w2, stack_, 
                                    config=moe_config, 
                                    lora_a_process_func=stack_, lora_b_process_func=stack_,
                                    lora_a_split_func=sp_0, lora_b_split_func=sp_id)
                ],
                config=moe_config)
            )

        return ModelWeightInfo(layer_weights=layer_weights, weights=weights)

class Mixtral(BaseModel):
    @staticmethod
    def get_weight_cls():
        return MixtralWeightInfo

    @classmethod
    def _create_config(cls, ckpt_path: str):
        config_path = os.path.join(ckpt_path, 'config.json')
        with open(config_path) as f:
            config_json = json.load(f)
        size_per_head=config_json['hidden_size'] // config_json['num_attention_heads']
        config = GptInitModelParameters(
            head_num=config_json['num_attention_heads'],
            size_per_head=size_per_head,
            inter_size=config_json['intermediate_size'],
            layer_num=config_json['num_hidden_layers'],
            max_seq_len=config_json.get('max_sequence_length', 2048),
            vocab_size=config_json['vocab_size'],
            head_num_kv = config_json['num_key_value_heads'],
            activation_type='SiGLU',
            norm_type='rmsnorm',
            rotary_embedding_dim=size_per_head,
            has_moe_norm = True,
            rotary_embedding_style=1,
            has_post_decoder_layernorm=True,
            rotary_embedding_base = config_json.get('rope_theta', 10000),
            expert_num = config_json['num_local_experts'],
            moe_k = config_json['num_experts_per_tok'],
            moe_style = 1,
            moe_layer_index = [i for i in range(config_json['num_hidden_layers'])])
        config.special_tokens.eos_token_id = 2
        config.special_tokens.bos_token_id = 1
        return config

register_model('mixtral', Mixtral, ['MixtralForCausalLM'])
