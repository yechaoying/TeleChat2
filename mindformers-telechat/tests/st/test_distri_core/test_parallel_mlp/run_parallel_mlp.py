# Copyright 2024 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""run parallel mlp"""

import argparse
import os

import numpy as np

import mindspore as ms
import mindspore.common.dtype as mstype
import mindspore.dataset as ds
import mindspore.nn as nn
import mindspore.ops as ops
from mindspore import Tensor
from mindspore.communication.management import init
from mindspore.nn import AdamWeightDecay, SoftmaxCrossEntropyWithLogits

from mindformers.experimental.parallel_core.pynative.config import ModelParallelConfig, TransformerConfig
from mindformers.experimental.parallel_core.pynative.parallel_state import initialize_model_parallel
from mindformers.experimental.parallel_core.pynative.transformer import ParallelMLP
from mindformers.modules import FeedForward
from mindformers.modules.transformer.op_parallel_config import default_dpmp_config

from tests.st.test_distri_core.utils import TestData, train, transform_mlp_golden_params_to_pynative_params, \
    generate_ckpt

class MLPNet(nn.Cell):
    """
    define a graph MLP net
    """
    def __init__(self,
                 hidden_size,
                 ffn_hidden_size,
                 hidden_act='gelu',
                 parallel_config=default_dpmp_config,
                 param_init_type=mstype.float32,
                 compute_dtype=mstype.float16):
        super(MLPNet, self).__init__()
        self.mlp = FeedForward(hidden_size=hidden_size,
                               ffn_hidden_size=ffn_hidden_size,
                               dropout_rate=0.,
                               hidden_act=hidden_act,
                               param_init_type=param_init_type,
                               parallel_config=parallel_config,
                               compute_dtype=compute_dtype)
        self.loss = SoftmaxCrossEntropyWithLogits()
        self.cast = ops.Cast()

    def construct(self, x, labels):
        output = self.mlp(x)
        output = ops.sum(output, dim=-1, keepdim=False)
        output = self.cast(output, mstype.float32)
        loss = self.loss(output, labels)
        return loss


class ParallelMLPNet(nn.Cell):
    """
    define a pynative MLP net
    """
    def __init__(self, config):
        super(ParallelMLPNet, self).__init__()
        self.mlp = ParallelMLP(config=config)
        self.loss = SoftmaxCrossEntropyWithLogits()
        self.cast = ops.Cast()
        self.dtype = config.compute_dtype

    def construct(self, x, labels):
        x = self.cast(x, self.dtype)
        output, _ = self.mlp(x)
        output = ops.sum(output, dim=-1, keepdim=False)
        output = self.cast(output, mstype.float32)
        loss = self.loss(output, labels)
        return loss


def generate_golden():
    """
    run graph mode mlp to generate golden ckpt and loss
    """
    batch_size = 1
    dataset_size = 3
    seq_length = 8
    hidden_size = 16
    ffn_hidden_size = 4 * hidden_size
    ms.set_context(device_target="Ascend",
                   mode=ms.GRAPH_MODE,
                   deterministic='ON',
                   jit_config={'jit_level': 'O0'})
    init()

    ms.reset_auto_parallel_context()
    ms.set_auto_parallel_context(parallel_mode=ms.ParallelMode.STAND_ALONE)

    ms.set_seed(2024)
    input_data = np.random.random((dataset_size, seq_length, hidden_size)).astype(np.float32)
    label_data = np.zeros((dataset_size, seq_length)).astype(np.float32)
    dataset = TestData(input_data=input_data, label_data=label_data)
    dataset = ds.GeneratorDataset(dataset, column_names=['input_ids', 'labels'])
    dataset = dataset.batch(batch_size)

    network = MLPNet(hidden_size=hidden_size,
                     ffn_hidden_size=ffn_hidden_size,
                     param_init_type=mstype.float32,
                     compute_dtype=mstype.float32)
    param_dict = generate_ckpt(hidden_size=hidden_size, module_type='mlp')
    ms.load_param_into_net(network, param_dict)
    save_golden = False
    if save_golden:
        ms.save_checkpoint(network, "mlp_golden.ckpt")
    optimizer = AdamWeightDecay(params=network.get_parameters())

    train(1, dataset, network, optimizer, None)


def run_parallel_mlp():
    """
    run pynative mode mlp and load golden ckpt to generate pynative loss
    """
    batch_size = 1
    dataset_size = 3
    seq_length = 8
    hidden_size = 16
    tensor_parallel = 2
    ffn_hidden_size = 4 * hidden_size
    ms.set_context(device_target="Ascend", mode=ms.PYNATIVE_MODE, deterministic='ON')

    init()
    initialize_model_parallel(tensor_model_parallel_size=tensor_parallel)

    ms.set_seed(2024)
    input_data = np.random.random((dataset_size, seq_length, hidden_size)).astype(np.float32)
    label_data = np.zeros((dataset_size, seq_length)).astype(np.float32)
    dataset = TestData(input_data=input_data, label_data=label_data)
    dataset = ds.GeneratorDataset(dataset, column_names=['input_ids', 'labels'])
    dataset = dataset.batch(batch_size)

    parallel_config = ModelParallelConfig(tensor_model_parallel_size=tensor_parallel)
    config = TransformerConfig(vocab_size=1,
                               num_layers=1,
                               num_attention_heads=1,
                               hidden_size=hidden_size,
                               ffn_hidden_size=ffn_hidden_size,
                               parallel_config=parallel_config,
                               mlp_has_bias=True,
                               hidden_act='gelu',
                               params_dtype='float32',
                               compute_dtype='float32')
    network = ParallelMLPNet(config=config)
    save_golden = False
    if save_golden:
        golden_ckpt_path = "mlp_golden.ckpt"
        assert os.path.exists(golden_ckpt_path), \
            "'golden.ckpt' did not exits, please run generate_golden() to generate one by running below command: \n" + \
            "`pytest -sv test_parallel_mlp.py::TestParallelMLP::generate_golden`"
        golden_params = ms.load_checkpoint(golden_ckpt_path)
        pynative_params = transform_mlp_golden_params_to_pynative_params(golden_params)
        param_not_load, _ = ms.load_param_into_net(network, pynative_params)
        assert not param_not_load, f"{param_not_load} was not loaded in this net, test failed."
    else:
        param_dict = generate_ckpt(hidden_size=hidden_size, module_type='transformerlayer')
        pynative_params = transform_mlp_golden_params_to_pynative_params(param_dict)
        ms.load_param_into_net(network, pynative_params)

    input_ids = Tensor(shape=(None, None, None), dtype=mstype.float32)
    labels = Tensor(shape=(None, None), dtype=mstype.float32)
    network.set_inputs(input_ids, labels)

    optimizer = AdamWeightDecay(params=network.get_parameters())

    losses = train(1, dataset, network, optimizer, None)
    losses = list(map(lambda x: x[0], losses))
    golden_losses = [228.6106, 493.6235, 470.0542]

    assert np.allclose(losses, golden_losses, atol=1.e-3, rtol=1.e-3)



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--generate_golden', action='store_true', help="Generate golden data for test."
    )

    args, rest_args = parser.parse_known_args()
    if args.generate_golden:
        generate_golden()
    else:
        run_parallel_mlp()