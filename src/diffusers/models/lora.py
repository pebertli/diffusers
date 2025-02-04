# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Optional
from collections import OrderedDict

import torch
import torch.nn.functional as F
from torch import nn

from ..loaders import PatchedLoraProjection, text_encoder_attn_modules, text_encoder_mlp_modules
from ..utils import logging


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


def adjust_lora_scale_text_encoder(text_encoder, lora_scale: float = 1.0):
    for _, attn_module in text_encoder_attn_modules(text_encoder):
        if isinstance(attn_module.q_proj, PatchedLoraProjection):
            attn_module.q_proj.lora_scale = lora_scale
            attn_module.k_proj.lora_scale = lora_scale
            attn_module.v_proj.lora_scale = lora_scale
            attn_module.out_proj.lora_scale = lora_scale

    for _, mlp_module in text_encoder_mlp_modules(text_encoder):
        if isinstance(mlp_module.fc1, PatchedLoraProjection):
            mlp_module.fc1.lora_scale = lora_scale
            mlp_module.fc2.lora_scale = lora_scale


class LoRALinearLayer(nn.Module):
    def __init__(self, in_features, out_features, rank=4, network_alpha=None, device=None, dtype=None):
        super().__init__()

        self.down = nn.Linear(in_features, rank, bias=False, device=device, dtype=dtype)
        self.up = nn.Linear(rank, out_features, bias=False, device=device, dtype=dtype)
        # This value has the same meaning as the `--network_alpha` option in the kohya-ss trainer script.
        # See https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning
        self.network_alpha = network_alpha
        self.rank = rank
        self.out_features = out_features
        self.in_features = in_features

        nn.init.normal_(self.down.weight, std=1 / rank)
        nn.init.zeros_(self.up.weight)

    def forward(self, hidden_states):
        orig_dtype = hidden_states.dtype
        dtype = self.down.weight.dtype

        down_hidden_states = self.down(hidden_states.to(dtype))
        up_hidden_states = self.up(down_hidden_states)

        if self.network_alpha is not None:
            up_hidden_states *= self.network_alpha / self.rank

        return up_hidden_states.to(orig_dtype)


class LoRAConv2dLayer(nn.Module):
    def __init__(
        self, in_features, out_features, rank=4, kernel_size=(1, 1), stride=(1, 1), padding=0, network_alpha=None
    ):
        super().__init__()

        self.down = nn.Conv2d(in_features, rank, kernel_size=kernel_size, stride=stride, padding=padding, bias=False)
        # according to the official kohya_ss trainer kernel_size are always fixed for the up layer
        # # see: https://github.com/bmaltais/kohya_ss/blob/2accb1305979ba62f5077a23aabac23b4c37e935/networks/lora_diffusers.py#L129
        self.up = nn.Conv2d(rank, out_features, kernel_size=(1, 1), stride=(1, 1), bias=False)

        # This value has the same meaning as the `--network_alpha` option in the kohya-ss trainer script.
        # See https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning
        self.network_alpha = network_alpha
        self.rank = rank

        nn.init.normal_(self.down.weight, std=1 / rank)
        nn.init.zeros_(self.up.weight)

    def forward(self, hidden_states):
        orig_dtype = hidden_states.dtype
        dtype = self.down.weight.dtype

        down_hidden_states = self.down(hidden_states.to(dtype))
        up_hidden_states = self.up(down_hidden_states)

        if self.network_alpha is not None:
            up_hidden_states *= self.network_alpha / self.rank

        return up_hidden_states.to(orig_dtype)


class LoRACompatibleConv(nn.Conv2d):
    """
    A convolutional layer that can be used with LoRA.
    """

    def __init__(self, *args, lora_layer: Optional[LoRAConv2dLayer] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lora_layer = lora_layer
        self._fused_params = OrderedDict()

    def set_lora_layer(self, lora_layer: Optional[LoRAConv2dLayer]):
        self.lora_layer = lora_layer

    def _fuse_lora(self, lora_scale=1.0, lora_name: str = None):
        if self.lora_layer is None:
            return

        # make sure lora have a unique name
        if lora_name is None:
            lora_name = "unspecified"
            unspecified_num = 0
            while lora_name in self._fused_params:
                lora_name = f"{lora_name}{unspecified_num+1}"
                unspecified_num += 1
        if lora_name in self._fused_params:
            raise ValueError(f"LoRA with name {lora_name} already fused")

        dtype, device = self.weight.data.dtype, self.weight.data.device

        w_orig = self.weight.data.float()
        w_up = self.lora_layer.up.weight.data.float()
        w_down = self.lora_layer.down.weight.data.float()

        if self.lora_layer.network_alpha is not None:
            w_up = w_up * self.lora_layer.network_alpha / self.lora_layer.rank

        fusion = torch.mm(w_up.flatten(start_dim=1), w_down.flatten(start_dim=1))
        fusion = fusion.reshape((w_orig.shape))
        fused_weight = w_orig + (lora_scale * fusion)
        self.weight.data = fused_weight.to(device=device, dtype=dtype)

        # we can drop the lora layer now
        self.lora_layer = None

        # offload the up and down matrices to CPU to not blow the memory
        self._fused_params[lora_name] = {
            "w_up": w_up.cpu(),
            "w_down": w_down.cpu(),
            "lora_scale": lora_scale
        }

    def _unfuse_lora(self, lora_name: str = None):
        if len(self._fused_params) == 0:
            return

        if lora_name is None:
            unfuse_lora = self._fused_params.popitem(last=True)[1]
        else:
            if lora_name not in self._fused_params:
                raise ValueError(f"LoRA with name {lora_name} not found")
            unfuse_lora = self._fused_params.pop(lora_name)

        fused_weight = self.weight.data
        dtype, device = fused_weight.data.dtype, fused_weight.data.device

        w_up = unfuse_lora["w_up"].to(device=device).float()
        w_down = unfuse_lora["w_down"].to(device).float()
        lora_scale = unfuse_lora["lora_scale"]

        fusion = torch.mm(w_up.flatten(start_dim=1), w_down.flatten(start_dim=1))
        fusion = fusion.reshape((fused_weight.shape))
        unfused_weight = fused_weight.float() - (lora_scale * fusion)
        self.weight.data = unfused_weight.to(device=device, dtype=dtype)


    def forward(self, hidden_states, scale: float = 1.0):
        if self.lora_layer is None:
            # make sure to the functional Conv2D function as otherwise torch.compile's graph will break
            # see: https://github.com/huggingface/diffusers/pull/4315
            return F.conv2d(
                hidden_states, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups
            )
        else:
            return super().forward(hidden_states) + (scale * self.lora_layer(hidden_states))


class LoRACompatibleLinear(nn.Linear):
    """
    A Linear layer that can be used with LoRA.
    """

    def __init__(self, *args, lora_layer: Optional[LoRALinearLayer] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lora_layer = lora_layer
        self._fused_params = OrderedDict()


    def set_lora_layer(self, lora_layer: Optional[LoRALinearLayer]):
        self.lora_layer = lora_layer

    def _fuse_lora(self, lora_scale=1.0, lora_name: str = None):
        if self.lora_layer is None:
            return

        if lora_name is None:
            lora_name = "unspecified"
            unspecified_num = 0
            while lora_name in self._fused_params:
                lora_name = f"{lora_name}{unspecified_num+1}"
                unspecified_num += 1
        if lora_name in self._fused_params:
            raise ValueError(f"LoRA with name {lora_name} already fused")

        dtype, device = self.weight.data.dtype, self.weight.data.device

        w_orig = self.weight.data.float()
        w_up = self.lora_layer.up.weight.data.float()
        w_down = self.lora_layer.down.weight.data.float()

        if self.lora_layer.network_alpha is not None:
            w_up = w_up * self.lora_layer.network_alpha / self.lora_layer.rank

        fused_weight = w_orig + (lora_scale * torch.bmm(w_up[None, :], w_down[None, :])[0])
        self.weight.data = fused_weight.to(device=device, dtype=dtype)

        # we can drop the lora layer now
        self.lora_layer = None

        # offload the up and down matrices to CPU to not blow the memory
        self._fused_params[lora_name] = {
            "w_up": w_up.cpu(),
            "w_down": w_down.cpu(),
            "lora_scale": lora_scale
        }

    def _unfuse_lora(self, lora_name: str = None):
        if len(self._fused_params) == 0:
            return

        if lora_name is None:
            unfuse_lora = self._fused_params.popitem(last=True)[1]

        else:
            if lora_name not in self._fused_params:
                raise ValueError(f"LoRA with name {lora_name} not found")
            unfuse_lora = self._fused_params.pop(lora_name)

        fused_weight = self.weight.data
        dtype, device = fused_weight.dtype, fused_weight.device

        w_up = unfuse_lora["w_up"].to(device=device).float()
        w_down = unfuse_lora["w_down"].to(device).float()
        lora_scale = unfuse_lora["lora_scale"]

        unfused_weight = fused_weight.float() - (lora_scale * torch.bmm(w_up[None, :], w_down[None, :])[0])
        self.weight.data = unfused_weight.to(device=device, dtype=dtype)


    def forward(self, hidden_states, scale: float = 1.0):
        if self.lora_layer is None:
            out = super().forward(hidden_states)
            return out
        else:
            out = super().forward(hidden_states) + (scale * self.lora_layer(hidden_states))
            return out
