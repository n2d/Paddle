#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import numpy as np
import os
from datetime import timedelta
from ..fluid.layer_helper import LayerHelper
from ..fluid.framework import Variable
from ..fluid.framework import in_dygraph_mode
from ..fluid.framework import OpProtoHolder
from ..fluid.framework import _non_static_mode
from ..fluid.framework import _in_legacy_dygraph
from ..fluid.framework import convert_np_dtype_to_dtype_
from ..fluid.framework import _varbase_creator
from ..fluid.data_feeder import convert_dtype
from ..fluid.data_feeder import check_variable_and_dtype
from ..fluid.data_feeder import check_type
from ..fluid.data_feeder import check_dtype
from ..fluid.layers.tensor import fill_constant
from ..fluid.layers import utils
from ..fluid.dygraph import layers
from ..fluid.dygraph.parallel import prepare_context
import paddle
import paddle.fluid as fluid
import paddle.fluid.core as core
from paddle import _C_ops
import paddle.fluid.dygraph_utils as dygraph_utils

__all__ = []


class ReduceOp:
    """
    Specify the type of operation used for element-wise reductions.
    It should be one of the following values:

        ReduceOp.SUM

        ReduceOp.MAX

        ReduceOp.MIN

        ReduceOp.PROD

    Examples:
        .. code-block:: python

            import numpy as np
            import paddle
            from paddle.distributed import ReduceOp
            from paddle.distributed import init_parallel_env

            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            init_parallel_env()
            if paddle.distributed.ParallelEnv().local_rank == 0:
                np_data = np.array([[4, 5, 6], [4, 5, 6]])
            else:
                np_data = np.array([[1, 2, 3], [1, 2, 3]])
            data = paddle.to_tensor(np_data)
            paddle.distributed.all_reduce(data, op=ReduceOp.SUM)
            out = data.numpy()
            # [[5, 7, 9], [5, 7, 9]]
    """
    SUM = 0
    MAX = 1
    MIN = 2
    PROD = 3
    AVG = 4


class Group():
    """
    The abstract representation of group.
    """

    def __init__(self, rank, rank_num, id=0, ranks=[], pg=None, name=None):
        self.rank = rank
        self.nranks = rank_num
        self.id = id
        self.ranks = ranks
        self.pg = pg
        self.name = name

    def is_member(self):
        if self.rank < 0:
            return False
        if self.nranks < 2:
            return False
        return True

    def get_group_rank(self, rank):
        if self.is_member() and rank in self.ranks:
            return self.ranks.index(rank)
        else:
            return -1

    @property
    def process_group(self):
        return self.pg

    def __repr__(self):
        debug_str = "rank: {}, nranks: {}, id: {}, ranks: ".format(
            self.rank, self.nranks, self.id)
        debug_str += ", ".join(map(str, self.ranks))
        debug_str += "; name: "
        debug_str += self.name if self.name else "None"
        return debug_str


_global_env = None


def _get_global_env():
    global _global_env
    if not _global_env:
        _global_env = paddle.distributed.ParallelEnv()
    return _global_env


# group map : the map of all group, 0 for GlobalGroup
# Dict[int, Group]
_group_map = {}

# group map by name : the map of all groups from their names
# Dict[name, Group]
_group_map_by_name = {}

# Name of the default group for init_parallel_env
_default_group_name = "_default_pg"

_valid_backend_list = ['nccl', 'gloo', 'hccl', 'heter']
_default_store = None  # the default tcp store
_default_backend = None


def _set_default_backend(backend):
    global _default_backend
    _default_backend = backend


def _set_default_store(store):
    global _default_store
    _default_store = store


def _get_group_map():
    global _group_map
    if not _group_map:
        genv = _get_global_env()
        _group_map[0] = Group(
            genv.rank, genv.world_size, ranks=list(range(genv.world_size)))
    return _group_map


def _get_global_group():
    return _get_group_map()[0]


def _get_group_map_by_name():
    global _group_map_by_name
    return _group_map_by_name


def _get_default_group():
    global _group_map_by_name
    assert _default_group_name in _group_map_by_name, (
        "Call paddle.distributed.init_parallel_env first "
        "to initialize the distributed environment.")
    return _get_group_map_by_name()[_default_group_name]


def _set_group_map(gid, group):
    global _group_map
    assert gid not in _group_map
    _group_map[gid] = group


def _set_group_map_by_name(name, group):
    global _group_map_by_name
    assert name not in _group_map_by_name
    _group_map_by_name[name] = group


def _new_ring_id():
    return len(_get_group_map()) + max(_get_global_env().nrings, 9)


def get_group(id=0):
    """

    Get group instance by group id.

    Args:
        id (int): the group id. Default value is 0.

    Returns:
        Group: the group instance.

    Examples:
        .. code-block:: python

            ...
            gid = paddle.distributed.new_group([2,4,6])
            paddle.distributed.get_group(gid.id)

    """

    gm = _get_group_map()
    return gm[id] if id in gm else None


def _new_process_group_impl(backend,
                            store,
                            rank,
                            world_size,
                            group_name,
                            pg_options,
                            group_id=0,
                            src_rank=None,
                            dst_rank=None):
    pg = None
    genv = _get_global_env()
    if backend != 'heter':
        assert src_rank is None and dst_rank is None, (
            "src_rank and dst_rank "
            "can only be set for heter backend.")
    assert backend in _valid_backend_list, "Unsupported backend: %s." % backend
    if backend == "gloo":
        place = core.CPUPlace()
        pg = core.ProcessGroupGloo(store, rank, world_size, place, group_id)
    elif backend == "nccl":
        place = core.CUDAPlace(genv.device_id)
        pg = core.ProcessGroupNCCL(store, rank, world_size, place, group_id)
    elif backend == "hccl":
        place = core.NPUPlace(genv.device_id)
        pg = core.ProcessGroupHCCL(store, rank, world_size, place, group_id)
    elif backend == "heter":
        place = None
        if core.is_compiled_with_cuda():
            place = core.CUDAPlace(genv.device_id)
        elif core.is_compiled_with_npu():
            place = core.NPUPlace(genv.device_id)
        cluster_id = int(os.getenv("CLUSTER_ID", "-1"))
        assert cluster_id >= 0, "please set the CLUSTER_ID variable."
        cluster_size = os.getenv("CLUSTER_SIZE", None)
        assert cluster_size, "please set the CLUSTER_SIZE variable."
        cluster_size = cluster_size.split(",")
        cluster_size = [int(s) for s in cluster_size]
        switch_ep = os.getenv("CLUSTER_SWITCH", None)
        assert switch_ep, "please set the CLUSTER_SWITCH variable."
        cluster_size_cumsum = np.cumsum(cluster_size)
        cluster_offset = 0 if cluster_id == 0 else cluster_size_cumsum[
            cluster_id - 1]
        global_rank = cluster_offset + rank
        global_world_size = cluster_size_cumsum[-1]
        pg = core.ProcessGroupHeter(
            store,
            rank=global_rank,
            world_size=global_world_size,
            place=place,
            gid=group_id,
            local_rank=rank,
            local_size=world_size,
            gloo_rank=cluster_id,
            gloo_size=len(cluster_size),
            with_switch=True,
            switch_endpoint=switch_ep,
            src_rank=src_rank,
            dst_rank=dst_rank)

    return pg


def barrier(group=None):
    """

    Barrier among all participators in the group.

    Args:
        group (Group): The group instance return by new_group or None for global default group.

    Returns:
        None.

    Examples:
        .. code-block:: python

            import paddle
            from paddle.distributed import init_parallel_env

            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            init_parallel_env()
            paddle.distributed.barrier()
    """
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        group = _get_default_group() if group is None else group
        task = group.process_group.barrier()
        task.wait()
        return

    ring_id = 0 if group is None else group.id

    temp = fill_constant([1], dtype="int32", value="1")
    if _non_static_mode():
        return _C_ops.barrier(temp, temp, 'ring_id', ring_id)

    op_type = 'barrier'

    if not isinstance(ring_id, int):
        raise ValueError("The type of 'group' for barrier must be int.")
    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [temp]},
        outputs={'Out': [temp]},
        attrs={'ring_id': ring_id})


# _custom_gid provides a way for users to
# set the group id, which is usually useful
# to be compatible with the static mode.
_custom_gid = None


def _set_custom_gid(gid):
    global _custom_gid
    _custom_gid = gid


def new_group(ranks=None, backend=None):
    """

    Creates a new distributed communication group.

    Args:
        ranks (list): The global ranks of group members.
        backend (str): The backend used to create group, only nccl is supported now.

    Returns:
        Group: The group instance.

    Examples:
        .. code-block:: python

            import paddle

            paddle.distributed.init_parallel_env()
            tindata = paddle.randn(shape=[2, 3])
            gp = paddle.distributed.new_group([2,4,6])
            paddle.distributed.all_reduce(tindata, group=gp, use_calc_stream=False)

    """
    global _custom_gid
    global _group_map
    if in_dygraph_mode():
        global _default_group_name
        gid = _custom_gid if _custom_gid else _new_ring_id()
        group_name = _default_group_name + str(gid)
        if backend != 'heter' and (ranks is None or len(ranks) > 1):
            global_group = _get_default_group()
            global_rank = global_group.rank
            global_ranks = global_group.ranks
            backend = _default_backend if backend is None else backend
            if ranks is None:
                ranks = global_ranks
            assert len(ranks) <= len(global_ranks), (
                "Size of new group must be less than or "
                "equal to that of the default global group.")
        size = len(ranks)
        ranks = sorted(ranks)
        if backend == 'heter' or (size > 1 and global_rank in ranks):
            rank = 0 if backend == 'heter' else ranks.index(global_rank)
            src_rank = ranks[0] if backend == 'heter' else None
            dst_rank = ranks[1] if backend == 'heter' else None
            pg = _new_process_group_impl(
                backend,
                _default_store,
                rank,
                size,
                group_name,
                pg_options=None,
                group_id=gid,
                src_rank=src_rank,
                dst_rank=dst_rank)
        else:
            rank = -1
            pg = None
        group = Group(rank, size, id=gid, ranks=ranks, pg=pg, name=group_name)
        _group_map_by_name[group_name] = group
        _group_map[gid] = group

        return group

    if not backend:
        backend = 'nccl'
    assert backend == 'nccl', ("backend other than nccl is not supported yet")

    genv = _get_global_env()
    global_rank = genv.rank

    ring_id = _new_ring_id()

    if global_rank not in ranks:
        gp = Group(-1, -1, ring_id, ranks)
        _group_map[ring_id] = gp
    else:
        ranks = sorted(ranks)
        group_rank = ranks.index(global_rank)
        group_size = len(ranks)
        gp = Group(group_rank, group_size, ring_id, ranks)
        _group_map[ring_id] = gp

        if group_size >= 2:
            strategy = core.ParallelStrategy()
            strategy.nranks = group_size
            strategy.local_rank = group_rank
            strategy.trainer_endpoints = [
                genv.trainer_endpoints[i] for i in ranks
            ]
            strategy.current_endpoint = genv.current_endpoint
            strategy.nrings = 1

            if core.is_compiled_with_cuda():
                place = core.CUDAPlace(genv.device_id)
                core.NCCLParallelContext(strategy,
                                         place).init_with_ring_id(ring_id)
            elif core.is_compiled_with_npu():
                place = core.NPUPlace(genv.device_id)
                core.HCCLParallelContext(strategy,
                                         place).init_with_ring_id(ring_id)
            elif core.is_compiled_with_mlu():
                place = core.MLUPlace(genv.device_id)
                core.CNCLParallelContext(strategy,
                                         place).init_with_ring_id(ring_id)
            else:
                assert False, ("no cuda device found")
        else:
            return gp

    # TODO(shenliang03): This is a temporary solution to solve the problem of 
    # hang caused by cross-creation of new_group
    tmp = paddle.to_tensor(
        [1], dtype="int32") if _non_static_mode() else fill_constant(
            [0], dtype="int32", value="1")
    paddle.distributed.all_reduce(tmp, use_calc_stream=True)
    paddle.distributed.wait(tmp)
    return gp


def wait(tensor, group=None, use_calc_stream=True):
    """

    wait to sync stream for group.

    Args:
        tensor (Tensor): The Tensor used before sync.
        group (Group): The Group instance to perform sync.
        use_calc_stream (bool): Wether to use calculation stream (True) or communication stream (False).
            Default to True.

    Returns:
        None.

    Examples:
        .. code-block:: python

            import paddle

            paddle.distributed.init_parallel_env()
            tindata = paddle.randn(shape=[2, 3])
            paddle.distributed.all_reduce(tindata, use_calc_stream=True)
            paddle.distributed.wait(tindata)

    """

    if group is not None and not group.is_member():
        return

    ring_id = 0 if group is None else group.id

    if use_calc_stream:
        _sync_calc_stream(tensor)
    else:
        _sync_comm_stream(tensor, ring_id)


def _sync_calc_stream(tensor):

    if _non_static_mode():
        return _C_ops.c_sync_calc_stream(tensor, tensor)

    op_type = 'c_sync_calc_stream'

    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [tensor]},
        outputs={'Out': [tensor]}, )


def _sync_comm_stream(tensor, ring_id=0):

    if _non_static_mode():
        return _C_ops.c_sync_comm_stream([tensor], [tensor], 'ring_id', ring_id)

    op_type = 'c_sync_comm_stream'

    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [tensor]},
        outputs={'Out': [tensor]},
        attrs={'ring_id': ring_id}, )


def broadcast(tensor, src, group=None, use_calc_stream=True):
    """

    Broadcast a tensor from the source to all others.
    As shown below, 4 GPUs each start 4 processes and GPU0 owns data 0. Through broadcast operator,
    the data 0 will be sent to all GPUs from GPU0.

    .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/broadcast.png
        :width: 800
        :alt: broadcast
        :align: center

    Args:
        tensor (Tensor): The Tensor to send if current rank is the source, or the tensor to receive otherwise. Its data type
            should be float16, float32, float64, int32 or int64.
        src (int): The source rank.
        group (Group): The group instance return by new_group or None for global default group.
        use_calc_stream (bool): Wether to use calculation stream (True) or communication stream (False).
            Default to True.

    Returns:
        None.

    Examples:
        .. code-block:: python

            # required: distributed
            import numpy as np
            import paddle
            from paddle.distributed import init_parallel_env

            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            init_parallel_env()
            if paddle.distributed.ParallelEnv().local_rank == 0:
                np_data = np.array([[4, 5, 6], [4, 5, 6]])
            else:
                np_data = np.array([[1, 2, 3], [1, 2, 3]])
            data = paddle.to_tensor(np_data)
            paddle.distributed.broadcast(data, 1)
            out = data.numpy()
            # [[1, 2, 3], [1, 2, 3]]
    """

    if group is not None and not group.is_member():
        return

    if not isinstance(src, int):
        raise ValueError("src should be int.")

    if in_dygraph_mode():
        group = _get_default_group() if group is None else group
        gsrc = group.get_group_rank(src)
        assert gsrc >= 0, ("src rank out of group, need global rank")
        task = group.process_group.broadcast(tensor, gsrc)
        if use_calc_stream:
            task.wait()
            return None
        else:
            return task

    ring_id = ring_id = 0 if group is None else group.id
    gsrc = src if group is None else group.get_group_rank(src)
    assert gsrc >= 0, ("src rank out of group, need global rank")

    if _non_static_mode():
        return _C_ops.c_broadcast(tensor, tensor, 'root', gsrc,
                                  'use_calc_stream', use_calc_stream, 'ring_id',
                                  ring_id)

    op_type = 'c_broadcast'
    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        'broadcast')

    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [tensor]},
        outputs={'Out': [tensor]},
        attrs={
            'root': gsrc,
            'use_calc_stream': use_calc_stream,
            'ring_id': ring_id,
        })


def all_reduce(tensor, op=ReduceOp.SUM, group=None, use_calc_stream=True):
    """

    Reduce a tensor over all ranks so that all get the result.
    As shown below, 4 GPUs each start 4 processes and the data on each GPU is represnted
    by the GPU number. The reduce operator is sum. Through all_reduce operator, 
    each GPU will have the sum of the data from all GPUs.

    .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/allreduce.png
        :width: 800
        :alt: all_reduce
        :align: center

    Args:
        tensor (Tensor): The input Tensor. It also works as the output Tensor. Its data type
            should be float16, float32, float64, int32 or int64.
        op (ReduceOp.SUM|ReduceOp.MAX|ReduceOp.Min|ReduceOp.PROD): Optional. The operation used. Default value is ReduceOp.SUM.
        group (Group): The group instance return by new_group or None for global default group.
        use_calc_stream (bool): Wether to use calculation stream (True) or communication stream (False).
            Default to True.

    Returns:
        None.

    Examples:
        .. code-block:: python

            # required: distributed
            import numpy as np
            import paddle
            from paddle.distributed import ReduceOp
            from paddle.distributed import init_parallel_env

            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            init_parallel_env()
            if paddle.distributed.ParallelEnv().local_rank == 0:
                np_data = np.array([[4, 5, 6], [4, 5, 6]])
            else:
                np_data = np.array([[1, 2, 3], [1, 2, 3]])
            data = paddle.to_tensor(np_data)
            paddle.distributed.all_reduce(data)
            out = data.numpy()
            # [[5, 7, 9], [5, 7, 9]]
    """
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        if op == ReduceOp.SUM:
            op_type = core.ReduceOp.SUM
        elif op == ReduceOp.MAX:
            op_type = core.ReduceOp.MAX
        elif op == ReduceOp.MIN:
            op_type = core.ReduceOp.MIN
        elif op == ReduceOp.PROD:
            op_type = core.ReduceOp.PRODUCT
        else:
            raise ValueError("Unknown reduce_op type for allreduce.")
        group = _get_default_group() if group is None else group
        task = group.process_group.allreduce(tensor, op_type)
        if use_calc_stream:
            task.wait()
            return None
        else:
            return task

    ring_id = 0 if group is None else group.id
    if _non_static_mode():
        if op == ReduceOp.SUM:
            return _C_ops.c_allreduce_sum_(tensor, 'use_calc_stream',
                                           use_calc_stream, 'ring_id', ring_id)
        elif op == ReduceOp.MAX:
            return _C_ops.c_allreduce_max_(tensor, 'use_calc_stream',
                                           use_calc_stream, 'ring_id', ring_id)
        elif op == ReduceOp.MIN:
            return _C_ops.c_allreduce_min_(tensor, 'use_calc_stream',
                                           use_calc_stream, 'ring_id', ring_id)
        elif op == ReduceOp.PROD:
            return _C_ops.c_allreduce_prod_(tensor, 'use_calc_stream',
                                            use_calc_stream, 'ring_id', ring_id)
        else:
            raise ValueError("Unknown parameter: {}.".format(op))

    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        'all_reduce')
    if op == ReduceOp.SUM:
        op_type = 'c_allreduce_sum'
    elif op == ReduceOp.MAX:
        op_type = 'c_allreduce_max'
    elif op == ReduceOp.MIN:
        op_type = 'c_allreduce_min'
    elif op == ReduceOp.PROD:
        op_type = 'c_allreduce_prod'
    if not isinstance(ring_id, int):
        raise ValueError("The type of 'ring_id' for all_reduce should be int.")
    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [tensor]},
        outputs={'Out': [tensor]},
        attrs={'ring_id': ring_id,
               'use_calc_stream': use_calc_stream})


def reduce(tensor, dst, op=ReduceOp.SUM, group=None, use_calc_stream=True):
    """

    Reduce a tensor to the destination from all others. As shown below, 4 GPUs each start 4 processes and the data on each GPU is respresnted
    by the GPU number. The destination of the reduce operator is GPU0 and the process is sum. Through reduce operator,
    the GPU0 will owns the sum of all data from all GPUs.

    .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/reduce.png
        :width: 800
        :alt: reduce
        :align: center

    Args:
        tensor (Tensor): The output Tensor for the destination and the input Tensor otherwise. Its data type
            should be float16, float32, float64, int32 or int64.
        dst (int): The destination rank id.
        op (ReduceOp.SUM|ReduceOp.MAX|ReduceOp.Min|ReduceOp.PROD): Optional. The operation used. Default value is ReduceOp.SUM.
        group (Group): The group instance return by new_group or None for global default group.
        use_calc_stream (bool): Wether to use calculation stream (True) or communication stream (False).
            Default to True.

    Returns:
        None.

    Examples:
        .. code-block:: python

            # required: distributed
            import numpy as np
            import paddle
            from paddle.distributed import init_parallel_env

            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            init_parallel_env()
            if paddle.distributed.ParallelEnv().local_rank == 0:
                np_data = np.array([[4, 5, 6], [4, 5, 6]])
            else:
                np_data = np.array([[1, 2, 3], [1, 2, 3]])
            data = paddle.to_tensor(np_data)
            paddle.distributed.reduce(data, 0)
            out = data.numpy()
            # [[5, 7, 9], [5, 7, 9]]
    """
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        if op == ReduceOp.SUM:
            op_type = core.ReduceOp.SUM
        elif op == ReduceOp.MAX:
            op_type = core.ReduceOp.MAX
        elif op == ReduceOp.MIN:
            op_type = core.ReduceOp.MIN
        elif op == ReduceOp.PROD:
            op_type = core.ReduceOp.PRODUCT
        else:
            raise ValueError("Unknown reduce_op type for reduce.")
        group = _get_default_group() if group is None else group
        gdst = group.get_group_rank(dst)
        assert gdst >= 0, ("dst rank out of group, need global rank")
        task = group.process_group.reduce(tensor, gdst, op_type)
        if use_calc_stream:
            task.wait()
            return None
        else:
            return task

    ring_id = 0 if group is None else group.id
    gdst = dst if group is None else group.get_group_rank(dst)
    assert gdst >= 0, ("dst rank out of group, need global rank")

    if _non_static_mode():
        if op == ReduceOp.SUM:
            return _C_ops.c_reduce_sum(tensor, tensor, 'use_calc_stream',
                                       use_calc_stream, 'ring_id', ring_id,
                                       'root_id', gdst)
        elif op == ReduceOp.MAX:
            return _C_ops.c_reduce_max(tensor, tensor, 'use_calc_stream',
                                       use_calc_stream, 'ring_id', ring_id,
                                       'root_id', gdst)
        elif op == ReduceOp.MIN:
            return _C_ops.c_reduce_min(tensor, tensor, 'use_calc_stream',
                                       use_calc_stream, 'ring_id', ring_id,
                                       'root_id', gdst)
        elif op == ReduceOp.PROD:
            return _C_ops.c_reduce_prod(tensor, tensor, 'use_calc_stream',
                                        use_calc_stream, 'ring_id', ring_id,
                                        'root_id', gdst)
        else:
            raise ValueError("Unknown parameter: {}.".format(op))

    op_type = 'c_reduce'
    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        'all_reduce')

    if op == ReduceOp.SUM:
        op_type = 'c_reduce_sum'
    elif op == ReduceOp.MAX:
        op_type = 'c_reduce_max'
    elif op == ReduceOp.MIN:
        op_type = 'c_reduce_min'
    elif op == ReduceOp.PROD:
        op_type = 'c_reduce_prod'

    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [tensor]},
        outputs={'Out': [tensor]},
        attrs={
            'ring_id': ring_id,
            'use_calc_stream': use_calc_stream,
            'root_id': gdst,
        })


def all_gather(tensor_list, tensor, group=None, use_calc_stream=True):
    """

    Gather tensors from all participators and all get the result. As shown
    below, 4 GPUs each start 4 processes and the data on each GPU is represnted
    by the GPU number. Through the all_gather operator, each GPU will have data
    from all GPUs.

    .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/allgather.png
        :width: 800
        :alt: all_gather
        :align: center

    Args:
        tensor_list (list): A list of output Tensors. Every element in the list must be a Tensor whose data type
            should be float16, float32, float64, int32 or int64.
        tensor (Tensor): The Tensor to send. Its data type
            should be float16, float32, float64, int32 or int64.
        group (Group): The group instance return by new_group or None for global default group.
        use_calc_stream (bool): Wether to use calculation stream (True) or communication stream (False).
            Default to True.

    Returns:
        None.

    Examples:
        .. code-block:: python

            # required: distributed
            import numpy as np
            import paddle
            from paddle.distributed import init_parallel_env

            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            init_parallel_env()
            tensor_list = []
            if paddle.distributed.ParallelEnv().local_rank == 0:
                np_data1 = np.array([[4, 5, 6], [4, 5, 6]])
                np_data2 = np.array([[4, 5, 6], [4, 5, 6]])
                data1 = paddle.to_tensor(np_data1)
                data2 = paddle.to_tensor(np_data2)
                paddle.distributed.all_gather(tensor_list, data1)
            else:
                np_data1 = np.array([[1, 2, 3], [1, 2, 3]])
                np_data2 = np.array([[1, 2, 3], [1, 2, 3]])
                data1 = paddle.to_tensor(np_data1)
                data2 = paddle.to_tensor(np_data2)
                paddle.distributed.all_gather(tensor_list, data2)
    """
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        group = _get_default_group() if group is None else group
        if len(tensor_list) == 0:
            tensor_shape = list(tensor.shape)
            tensor_shape[0] *= group.nranks
            out = paddle.empty(tensor_shape, tensor.dtype)
        else:
            out = paddle.concat(tensor_list, axis=0)
        task = group.process_group.all_gather(tensor, out)
        task.wait()
        tensor_list.clear()
        tensor_list.extend(paddle.split(out, group.nranks, 0))
        return

    ring_id = 0 if group is None else group.id
    nranks = _get_global_group().nranks if group is None else group.nranks

    if _non_static_mode():
        out = _C_ops.c_allgather(tensor, 'use_calc_stream', use_calc_stream,
                                 'ring_id', ring_id, 'nranks', nranks)
    else:
        op_type = 'c_allgather'
        helper = LayerHelper(op_type, **locals())
        out = helper.create_variable_for_type_inference(dtype=tensor.dtype)
        if not isinstance(tensor_list, list):
            raise ValueError("The type of 'tensor_list' for all_gather "
                             "should be list.")
        for elem in tensor_list:
            check_variable_and_dtype(
                elem, 'tensor_list',
                ['float16', 'float32', 'float64', 'int32', 'int64'],
                'all_gather')
        check_variable_and_dtype(
            tensor, 'tensor',
            ['float16', 'float32', 'float64', 'int32', 'int64'], 'all_gather')
        helper.append_op(
            type=op_type,
            inputs={'X': [tensor]},
            outputs={'Out': [out]},
            attrs={
                'ring_id': ring_id,
                'use_calc_stream': use_calc_stream,
                'nranks': nranks
            })

    tensor_list.extend(paddle.split(out, nranks, 0))


def scatter(tensor, tensor_list=None, src=0, group=None, use_calc_stream=True):
    """

    Scatter a tensor to all participators. As shown below, 4 GPUs each start 4 processes and the source of the scatter
    is GPU0. Through scatter operator, the data in GPU0 will be sent to all GPUs averagely.

    .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/scatter.png
        :width: 800
        :alt: scatter
        :align: center

    Args:
        tensor (Tensor): The output Tensor. Its data type
            should be float16, float32, float64, int32 or int64.
        tensor_list (list|tuple): A list/tuple of Tensors to scatter. Every element in the list must be a Tensor whose data type
            should be float16, float32, float64, int32 or int64. Default value is None.
        src (int): The source rank id. Default value is 0.
        group (Group): The group instance return by new_group or None for global default group.
        use_calc_stream (bool): Wether to use calculation stream (True) or communication stream (False).
            Default to True.

    Returns:
        None.

    Examples:
        .. code-block:: python

            # required: distributed
            import numpy as np
            import paddle
            from paddle.distributed import init_parallel_env

            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            init_parallel_env()
            if paddle.distributed.ParallelEnv().local_rank == 0:
                np_data1 = np.array([7, 8, 9])
                np_data2 = np.array([10, 11, 12])
            else:
                np_data1 = np.array([1, 2, 3])
                np_data2 = np.array([4, 5, 6])
            data1 = paddle.to_tensor(np_data1)
            data2 = paddle.to_tensor(np_data2)
            if paddle.distributed.ParallelEnv().local_rank == 0:
                paddle.distributed.scatter(data1, src=1)
            else:
                paddle.distributed.scatter(data1, tensor_list=[data1, data2], src=1)
            out = data1.numpy()
    """
    if group is not None and not group.is_member():
        return

    if not isinstance(src, int):
        raise ValueError("src should be int.")

    if in_dygraph_mode():
        group = _get_default_group() if group is None else group
        gsrc = group.get_group_rank(src)
        rank = group.rank
        nranks = group.nranks
    else:
        ring_id = 0 if group is None else group.id
        gsrc = src if group is None else group.get_group_rank(src)
        rank = _get_global_group().rank if group is None else group.rank
        nranks = _get_global_group().nranks if group is None else group.nranks
    assert gsrc >= 0, ("src rank out of group, need global rank")

    if rank != gsrc:
        tensor_list = []
        for _ in range(nranks):
            tensor_list.append(tensor)
    temp = paddle.concat(tensor_list, axis=0)
    if in_dygraph_mode():
        task = group.process_group.scatter(temp, tensor, gsrc)
        if use_calc_stream:
            task.wait()
            return None
        else:
            return task

    if _non_static_mode():
        return _C_ops.c_scatter(temp, tensor, 'use_calc_stream',
                                use_calc_stream, 'ring_id', ring_id, 'nranks',
                                nranks, 'root', gsrc)
    op_type = 'c_scatter'
    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        'scatter')
    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [temp]},
        outputs={'Out': [tensor]},
        attrs={
            'ring_id': ring_id,
            'root': gsrc,
            'use_calc_stream': use_calc_stream,
            'nranks': nranks,
        })


def _c_identity(tensor, group=None):
    """
    Return a copy of the tensor, mainly used with model parallel.

    Args:
        tensor (Tensor): The input Tensor. Its data type
            should be float16, float32, float64, int32 or int64.
        group (int): The id of the process group to work on.

    Returns:
        Tensor.
    """
    if group is not None and not group.is_member():
        return
    ring_id = 0 if group is None else group.id

    if _non_static_mode():
        return _C_ops.c_identity(tensor, 'use_calc_stream', True, 'ring_id',
                                 ring_id, 'use_model_parallel', True)
    op_type = 'c_identity'
    helper = LayerHelper(op_type, **locals())
    out = helper.create_variable_for_type_inference(dtype=tensor.dtype)

    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        '_c_identity')

    helper.append_op(
        type=op_type,
        inputs={'X': tensor},
        outputs={'Out': out},
        attrs={
            'ring_id': ring_id,
            'use_calc_stream': True,
            'use_model_parallel': True,
        })
    return out


def _c_concat(tensor, group=None):
    """
    Return allgather of the tensor, mainly used with model parallel.

    Args:
        tensor (Tensor): The input Tensor. Its data type
            should be float16, float32, float64, int32 or int64.
        group (int): The id of the process group to work on.

    Returns:
        Tensor.
    """
    if group is not None and not group.is_member():
        return
    group = _get_default_group() if group is None else group
    ring_id = group.id

    global_rank = _get_global_env().rank
    rank = group.rank
    nranks = group.nranks

    if _non_static_mode():
        return _C_ops.c_concat(tensor, 'ring_id', ring_id, 'use_calc_stream',
                               True, 'rank', rank, 'nranks', nranks,
                               'use_model_parallel', True)

    op_type = 'c_concat'
    helper = LayerHelper(op_type, **locals())
    out = helper.create_variable_for_type_inference(dtype=tensor.dtype)

    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        '_c_concat')

    helper.append_op(
        type=op_type,
        inputs={'X': tensor},
        outputs={'Out': out},
        attrs={
            'ring_id': ring_id,
            'use_calc_stream': True,
            'use_model_parallel': True,
            'nranks': nranks,
            'rank': rank
        })
    return out


def _c_split(tensor, group=None):
    """
    Split tensor evenly among all members, mainly used with model parallel.

    Args:
        tensor (Tensor): The input Tensor. Its data type
            should be float16, float32, float64, int32 or int64.
        rank (int): The rank of the current process.
        group (int): The id of the process group to work on.

    Returns:
        Tensor.
    """
    if group is not None and not group.is_member():
        return
    ring_id = 0 if group is None else group.id

    global_rank = _get_global_env().rank
    rank = global_rank if group is None else group.get_group_rank(global_rank)
    nranks = _get_global_env().world_size if group is None else group.nranks

    if _non_static_mode():
        return _C_ops.c_split(tensor, 'use_calc_stream', True, 'ring_id',
                              ring_id, 'rank', rank, 'nranks', nranks,
                              'use_model_parallel', True)

    op_type = 'c_split'
    helper = LayerHelper(op_type, **locals())
    out = helper.create_variable_for_type_inference(dtype=tensor.dtype)

    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        '_c_split')

    helper.append_op(
        type=op_type,
        inputs={'X': tensor},
        outputs={'Out': out},
        attrs={
            'ring_id': ring_id,
            'use_calc_stream': True,
            'rank': rank,
            'nranks': nranks,
            'use_model_parallel': True,
        })
    return out


def _mp_allreduce(tensor,
                  op=ReduceOp.SUM,
                  group=None,
                  use_calc_stream=True,
                  use_model_parallel=True):
    """[it is same as allreduce above, but it supports model parallel. And it support inplace startegy]
    """
    if group is not None and not group.is_member():
        return
    ring_id = 0 if group is None else group.id

    if in_dygraph_mode():
        assert op == ReduceOp.SUM, "Unknown parameter: {}.".format(op)

        from paddle.autograd import EagerPyLayer

        class mp_allreduce_eager(EagerPyLayer):
            @staticmethod
            def forward(ctx, tensor, use_calc_stream, ring_id,
                        use_model_parallel):
                ctx.ring_id = ring_id
                return _C_ops.c_allreduce_sum_(
                    tensor, 'use_calc_stream', use_calc_stream, 'ring_id',
                    ring_id, "use_model_parallel", use_model_parallel)

            @staticmethod
            def backward(ctx, dy):
                return _C_ops.c_identity(dy, 'use_calc_stream', True, 'ring_id',
                                         ctx.ring_id, 'use_model_parallel',
                                         True)

        return mp_allreduce_eager.apply(tensor, use_calc_stream, ring_id,
                                        use_model_parallel)

    elif _in_legacy_dygraph():
        if op == ReduceOp.SUM:
            return _C_ops.c_allreduce_sum_(
                tensor, 'use_calc_stream', use_calc_stream, 'ring_id', ring_id,
                "use_model_parallel", use_model_parallel)
        else:
            raise ValueError("Unknown parameter: {}.".format(op))

    op_type = 'c_allreduce_sum'
    helper = LayerHelper(op_type, **locals())
    out = helper.create_variable_for_type_inference(dtype=tensor.dtype)

    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        op_type)

    helper.append_op(
        type=op_type,
        inputs={'X': tensor},
        outputs={'Out': out},
        attrs={
            'ring_id': ring_id,
            'use_calc_stream': use_calc_stream,
            'use_model_parallel': use_model_parallel,
        })
    return out


def _c_lookup_table(table, index, start_index=0, name=None):
    """
    Lookup table according to index.

    Args:
        table (Tensor): The input Tensor. Its data type
            should be float16, float32, float64.
        index (Tensor): The index to lookup table.
        start_index (int): The initial index for table range.
        name (string): The name of the api

    Returns:
        Tensor.
    """
    if _non_static_mode():
        return _C_ops.c_embedding(table, index, "start_index", start_index)

    op_type = 'c_embedding'
    helper = LayerHelper(op_type, **locals())
    dtype = helper.input_dtype(input_param_name='table')
    check_variable_and_dtype(index, 'input', ['int32', 'int64'], op_type)
    tmp = helper.create_variable_for_type_inference(dtype)
    helper.append_op(
        type='c_embedding',
        inputs={'Ids': index,
                'W': table},
        outputs={'Out': tmp},
        attrs={"start_index": start_index})
    return tmp


class _Linear(layers.Layer):
    """
    Linear
    """

    def __init__(self,
                 in_features,
                 out_features,
                 weight_attr=None,
                 bias_attr=None,
                 name=None):
        super(_Linear, self).__init__()
        self._dtype = self._helper.get_default_dtype()
        self._weight_attr = weight_attr
        self._bias_attr = bias_attr
        self.weight = self.create_parameter(
            shape=[in_features, out_features],
            attr=self._weight_attr,
            dtype=self._dtype,
            is_bias=False)
        self.bias = self.create_parameter(
            shape=[out_features],
            attr=self._bias_attr,
            dtype=self._dtype,
            is_bias=True)
        self.name = name

    def forward(self, input):
        out = _linear(
            x=input, weight=self.weight, bias=self.bias, name=self.name)
        return out

    def extra_repr(self):
        name_str = ', name={}'.format(self.name) if self.name else ''
        return 'in_features={}, out_features={}, dtype={}{}'.format(
            self.weight.shape[0], self.weight.shape[1], self._dtype, name_str)


def _c_softmax_with_cross_entropy(logits,
                                  label,
                                  group=None,
                                  return_softmax=False):
    if group is not None and not group.is_member():
        return
    ring_id = 0 if group is None else group.id
    global_rank = _get_global_env().rank
    rank = global_rank if group is None else group.get_group_rank(global_rank)
    nranks = _get_global_env().world_size if group is None else group.nranks

    input_dims = len(list(logits.shape))
    label_dims = len(list(label.shape))
    if input_dims - 1 != label_dims and input_dims != label_dims:
        raise ValueError(
            'Expected nput_dims - 1 = label_dims or input_dims == label_dims\
             (got nput_dims{}, label_dims{})'.format(input_dims, label_dims))
    if input_dims - 1 == label_dims:
        label = paddle.unsqueeze(label, axis=-1)

    if _non_static_mode():
        softmax, loss = _C_ops.c_softmax_with_cross_entropy(
            logits, label, 'ring_id', ring_id, 'rank', rank, 'nranks', nranks)
        if not return_softmax:
            return loss
        else:
            return loss, softmax

    attrs = {
        'ring_id': ring_id,
        'rank': rank,
        'nranks': nranks,
    }
    helper = LayerHelper('c_softmax_with_cross_entropy', **locals())
    softmax = helper.create_variable_for_type_inference(dtype=logits.dtype)
    loss = helper.create_variable_for_type_inference(dtype=logits.dtype)
    helper.append_op(
        type='c_softmax_with_cross_entropy',
        inputs={'Logits': logits,
                'Label': label},
        outputs={'Softmax': softmax,
                 'Loss': loss},
        attrs=attrs)

    if return_softmax:
        return loss, softmax

    return loss


def _linear(x, weight, bias=None, name=None):
    """
    Fuction Linear
    """
    if _non_static_mode():
        pre_bias = _varbase_creator(dtype=x.dtype)
        _C_ops.matmul(x, weight, pre_bias, 'transpose_X', False, 'transpose_Y',
                      False, "alpha", 1)
        return dygraph_utils._append_bias_in_dygraph(
            pre_bias, bias, axis=len(x.shape) - 1)
    else:
        helper = LayerHelper('linear', **locals())
        dtype = x.dtype
        assert len(
            x.shape) < 4, "X latitude is not supported greater than 3 now."

        check_variable_and_dtype(x, 'x', ['float16', 'float32', 'float64'],
                                 'linear')
        check_dtype(dtype, 'dtype', ['float16', 'float32', 'float64'], 'linear')

        inputs = {'X': [x], 'Y': [weight]}
        attrs = {
            'transpose_X': False,
            'transpose_Y': False,
            'alpha': 1,
        }
        tmp = helper.create_variable_for_type_inference(dtype)
        helper.append_op(
            type='matmul_v2', inputs=inputs, outputs={'Out': tmp}, attrs=attrs)
        if bias is not None:
            res = helper.create_variable_for_type_inference(dtype)
            helper.append_op(
                type='elementwise_add',
                inputs={'X': [tmp],
                        'Y': [bias]},
                outputs={'Out': [res]},
                attrs={'axis': len(x.shape) - 1})
        else:
            res = tmp
        return res


def _set_var_distributed(var):
    if var is None:
        return

    var.is_distributed = True

    # NOTE: use current_block and find_var_recursive to support while_loop
    startup_block = paddle.static.default_startup_program().current_block()
    main_block = paddle.static.default_main_program().current_block()
    startup_block._find_var_recursive(var.name).is_distributed = True
    main_block._find_var_recursive(var.name).is_distributed = True


def _parallel_linear(x,
                     num_rows,
                     num_cols,
                     axis,
                     param_attr,
                     bias_attr,
                     gather_out,
                     inner_rank,
                     nranks,
                     split_tensor,
                     name,
                     group=None):
    """
    Parallel Linear

    axis the dimension of the parameter of linear layer. 
    axis = 0: the row dimension
    axis = 1: the col dimension
    
    """
    if group is not None and not group.is_member():
        return
    ring_id = 0 if group is None else group.id

    if axis == 0:
        if split_tensor:
            x = _c_split(x, group=group)
    else:
        x = _c_identity(x, group=group)

    linear = paddle.nn.Linear(
        num_rows,
        num_cols,
        weight_attr=param_attr,
        bias_attr=bias_attr,
        name=name)

    # NOTE: npu linear function use matmul_v2 but linear use matmul
    linear_function = _linear if core.is_compiled_with_npu()\
        else paddle.nn.functional.linear
    linear_out = linear_function(
        x,
        linear.weight,
        # NOTE(wangxi): row split, bias need add after allreduce
        None if axis == 0 else linear.bias,
        linear.name)

    _set_var_distributed(linear.weight)
    # set is_distributed for splited bias
    # if a linear layer is splited by row, each rank would hold a complete bias and they should be the same in each rank.
    # if a linear layer is splited by col, the bias would also be split into each rank as its weight
    if axis == 1 and linear._bias_attr != False:
        _set_var_distributed(linear.bias)

    if not gather_out: return linear_out

    out_shape = list(linear_out.shape)
    out_shape[0] *= 1 if axis == 0 else nranks
    main_block = paddle.static.default_main_program().current_block()
    out = main_block.create_var(
        shape=out_shape,
        dtype=linear_out.dtype,
        type=linear_out.type,
        lod_level=linear_out.lod_level,
        persistable=False,
        is_data=False,
        need_check_feed=linear_out.desc.need_check_feed())
    if axis == 0:
        main_block.append_op(
            type='c_allreduce_sum',
            inputs={'X': linear_out},
            outputs={'Out': out},
            attrs={
                'ring_id': ring_id,
                'use_calc_stream': True,
                'use_model_parallel': True
            })
        if linear.bias is not None:
            out = out + linear.bias
    else:
        main_block.append_op(
            type='c_concat',
            inputs={'X': linear_out},
            outputs={'Out': out},
            attrs={
                'rank': inner_rank,
                'ring_id': ring_id,
                'nranks': nranks,
                'use_calc_stream': True,
                'use_model_parallel': True
            })
    return out


def _parallel_embedding(x,
                        per_part_embeddings,
                        origin_size,
                        param_attr,
                        inner_rank,
                        num_partitions,
                        name,
                        group=None):
    """
    Parallel Embedding
    """
    if group is not None and not group.is_member():
        return
    ring_id = 0 if group is None else group.id

    helper = LayerHelper("_parallel_embedding", **locals())

    per_part_size = per_part_embeddings
    rank = inner_rank

    vocab_start_index = rank * per_part_size
    dtype = helper.get_default_dtype()
    size = [per_part_size, origin_size[1]]

    weight = helper.create_parameter(
        attr=param_attr, shape=size, dtype=dtype, is_bias=False)

    if num_partitions == 1:
        return paddle.nn.functional.embedding(
            x, weight=weight, padding_idx=None, sparse=False, name=name)

    startup_block = paddle.static.default_startup_program().global_block()
    main_block = paddle.static.default_main_program().global_block()
    startup_block.vars[weight.name].is_distributed = True
    main_block.vars[weight.name].is_distributed = True

    output_parallel = paddle.distributed.collective._c_lookup_table(
        weight, x, start_index=vocab_start_index, name=name)
    out = paddle.distributed.collective._mp_allreduce(
        output_parallel,
        group=group,
        use_calc_stream=True,
        use_model_parallel=True)
    return out


def split(x,
          size,
          operation,
          axis=0,
          num_partitions=1,
          gather_out=True,
          weight_attr=None,
          bias_attr=None,
          name=None):
    """

    Split the weight of the specified operation into multiple devices
    and do the computation in parallel.

    Now the following three cases are supported.

    Case 1: Parallel Embedding
        The weight of the embedding operation is a NxM matrix with N rows and M columns.
        With parallel embedding, the weight is split into num_partitions partitions, each
        of which is a matrix with (N/num_partitions + 1) rows and M column where the last
        row as the padding idx.

        Suppose we split the NxM weight into two partitons on device_0 and device_1
        respectively. Then, one each device, the final weight has (N/2 + 1) rows with the
        index range from 0 to N/2. On device_0, all values in the input within [0, N/2 -1]
        keep unchanged and all other values are changed to N/2 which is the padding index and
        are mapped to all zeros after embedding. In the same way, on device_1, the value V in the
        input within [N/2, N-1] will be changed to (V - N/2), and all other values are changed
        to N/2 and are mapped to all zeros after embedding. Finally, the results on the two
        devices are sum-reduced.

        The Embedding put on single card is as shown below:

        .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/split_embedding_single.png
            :width: 800
            :height: 350
            :alt: single_embedding
            :align: center

        Parallel Embedding is shown as below:

        .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/split_embedding_split.png
            :width: 800
            :alt: split_embedding
            :align: center

    Case 2: Row Parallel Linear
        The weight of the linear operation is a NxM matrix with N rows and M columns.
        With row parallel linear, the weight is split into num_partitions partitions, each
        of which is a matrix with N/num_partitions rows and M column.

        The linear layer put on single card is shown as below, the input variable is represented by X,
        the weight matrix is represented by W and the output vaiable is O. The linear layer on single card is 
        simple matrix multiplication operation, O = X * W.

        .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/split_single.png
            :width: 800
            :alt: single_linear
            :align: center

        Row Parallel Linear is shown as below. As the name suggests, Row Parallel Linear splits the weight matrix W into
        [[W_row1], [W_row2]] along the row. And accordingly the input is splitted along the column into [X_col1, X_col2] and multiply their
        respective weight matrices. Finally apply AllReduce on the output from each card to get the final output.

        .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/split_row.png
            :width: 800
            :alt: split_row
            :align: center

    Case 3: Column Parallel Linear
        The weight of the linear operation is a NxM matrix with N rows and M columns.
        With column parallel linear, the weight is split into num_paratitions partitions, each
        of which is a matrix with N rows and M/num_partitions column.

        The linear layer put on single card has been illustrated on case 2 and Column Parallel Linear
        is shown as below. The Column Parallel Linear splits the weight matrix W into [W_col1, W_col2] along the column and 
        these splitted matrices respectively multiply the input. Finally apply AllGather on the output from each card to get the final output. 

        .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/split_col.png
            :width: 800
            :alt: split_col
            :align: center
    
    As observed, the column parallel linear and row parallel linear can be combined to skip one ALLGATHER communication
    operator. Furthermore the Attention and MLP can be combined to imporve the performance as shown below.

    .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/split_col_row.png
            :width: 800
            :alt: split_col_row
            :align: center

    Args:
        x (Tensor): Input tensor. It's data type should be float16, float32, float64, int32 or int64.
        size (list|tuple): A list or tuple with two elements indicating the shape of the weight.
        operation (str): The name of the operation. The supported operations are 'linear' and 'embedding'.
        axis (int, Optional): Indicate along which axis to split the weight. Default: 0.
        num_partitions (int, Optional): How many parts the weight is partitioned. Default: 1.
        gather_out (bool, Optional): Whether to gather the output after computation. By default, the output
            on each partitions will be gathered after computation. Default: True.
        weight_attr (ParamAttr, Optional): The parameter attribute for the learnable
            weights(Parameter) of the specified operation. Default: None.
        bias_attr (ParamAttr, Optional): The parameter attribute for the bias
            of the specified operation. Default: None.
        name (str, Optional): The default value is None. Normally there is no need for user to set this
            property. Default: None. For more information, please refer to :ref:`api_guide_Name`.

    Returns:
        Tensor.

    Examples:
        .. code-block:: python

            # required: distributed
            import paddle
            import paddle.distributed.fleet as fleet

            paddle.enable_static()
            paddle.set_device('gpu:%d'%paddle.distributed.ParallelEnv().dev_id)
            fleet.init(is_collective=True)
            data = paddle.randint(0, 8, shape=[10,4])
            emb_out = paddle.distributed.split(
                data,
                (8, 8),
                operation="embedding",
                num_partitions=2)

    """
    assert isinstance(size, (list, tuple)), (
        "The type of size for "
        "paddle.distributed.split must be list or tuple.")
    assert len(size) == 2, ("Number of elements in size of "
                            "paddle.distributed.split must be two.")
    assert isinstance(operation, str), ("The type of operation for "
                                        "paddle.distributed.split must be str.")
    supported_operations = [
        'linear',
        'embedding',
    ]
    assert operation in supported_operations, (
        "The operation for "
        "paddle.distributed.split must be one of {}.".format(
            supported_operations))
    if _non_static_mode():
        raise ValueError(
            "paddle.distributed.split cannot be used in dynamic "
            "graph mode, plese use ParallelEmbedding, ParallelRowLinear, "
            "ParallelColumnLinear instead.")
    else:
        from .fleet import fleet
        assert fleet._role_maker, ("To use paddle.distributed.split, "
                                   "you must call fleet.init() firstly.")
        rank = fleet.worker_index()
        nranks = fleet.worker_num()

    # rank within a model parallel group
    inner_rank = rank % num_partitions

    if operation == "embedding":
        assert axis == 0, ("We only support to split the weight of embedding "
                           "along the first axis now.")
        assert size[0] % num_partitions == 0, \
            "The length of the vocabulary must be divisible by num_partitions " \
            "but received vocabulary={} num_partitions={}".format(size[0], num_partitions)

        per_part_size = size[0] // num_partitions
        emb_out = _parallel_embedding(
            x,
            per_part_size,
            size,
            weight_attr,
            inner_rank,
            num_partitions,
            name,
            group=None)
        return emb_out
    else:
        should_split = False
        if axis == 0:
            assert size[0] % num_partitions == 0, (
                "Number of rows of the weight for linear ({}) must be"
                " divisible by num_partitions ({})".format(size[0],
                                                           num_partitions))
            per_part_size = size[0] // num_partitions
            linear_size = (per_part_size, size[1])
            if x.shape[-1] == size[0]: should_split = True

        elif axis == 1:
            assert size[1] % num_partitions == 0, (
                "Number of column of the weight for linear ({}) must be"
                " divisible by num_partitions ({})".format(size[1],
                                                           num_partitions))
            per_part_size = size[1] // num_partitions
            linear_size = (size[0], per_part_size)
        else:
            raise ValueError("The value of axis must be 0 or 1, but the value "
                             "given is {}.".format(axis))

        linear_out = _parallel_linear(
            x,
            linear_size[0],
            linear_size[1],
            axis,
            weight_attr,
            bias_attr,
            gather_out,
            inner_rank,
            num_partitions,
            should_split,
            name=name,
            group=None)
        return linear_out


def alltoall(in_tensor_list, out_tensor_list, group=None, use_calc_stream=True):
    """
    Scatter tensors in in_tensor_list to all participators averagely and gather the result tensors in out_tensor_list.
    As shown below, the in_tensor_list in GPU0 includes 0_0 and 0_1, and GPU1 includes 1_0 and 1_1.
    Through alltoall operator, the 0_0 in GPU0 will be sent to GPU0 and 0_1 to GPU1, 1_0 in GPU1 sent to GPU0 and 1_1 to GPU1.
    Finally the out_tensor_list in GPU0 includes 0_0 and 1_0, and GPU1 includes 0_1 and 1_1.

    .. image:: https://githubraw.cdn.bcebos.com/PaddlePaddle/docs/develop/docs/api/paddle/distributed/img/alltoall.png
        :width: 800
        :alt: alltoall
        :align: center

    Args:
        in_tensor_list (list): A list of input Tensors. Every element in the list must be a Tensor whose data type
            should be float16, float32, float64, int32 or int64.
        out_tensor_list (Tensor): A list of output Tensors. The data type of its elements should be the same as the
            data type of the input Tensors.
        group (Group, optional): The group instance return by new_group or None for global default group. Default: None.
        use_calc_stream (bool, optional): Wether to use calculation stream (True) or communication stream. Default: True.
    
    Returns:
        None.
    
    Examples:
        .. code-block:: python

            # required: distributed
            import numpy as np
            import paddle
            from paddle.distributed import init_parallel_env
            
            init_parallel_env()
            out_tensor_list = []
            if paddle.distributed.ParallelEnv().rank == 0:
                np_data1 = np.array([[1, 2, 3], [4, 5, 6]])
                np_data2 = np.array([[7, 8, 9], [10, 11, 12]])
            else:
                np_data1 = np.array([[13, 14, 15], [16, 17, 18]])
                np_data2 = np.array([[19, 20, 21], [22, 23, 24]])
            data1 = paddle.to_tensor(np_data1)
            data2 = paddle.to_tensor(np_data2)
            paddle.distributed.alltoall([data1, data2], out_tensor_list)
            # out for rank 0: [[[1, 2, 3], [4, 5, 6]], [[13, 14, 15], [16, 17, 18]]]
            # out for rank 1: [[[7, 8, 9], [10, 11, 12]], [[19, 20, 21], [22, 23, 24]]]
    """
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        group = _get_default_group() if group is None else group
    else:
        ring_id = 0 if group is None else group.id

    temp = paddle.concat(in_tensor_list, axis=0)
    nranks = len(in_tensor_list)
    if in_dygraph_mode():
        if len(out_tensor_list) == 0:
            tensor_shape = list(in_tensor_list[0].shape)
            tensor_shape[0] *= nranks
            out = paddle.empty(tensor_shape, in_tensor_list[0].dtype)
        else:
            out = paddle.concat(out_tensor_list, axis=0)
        task = group.process_group.alltoall(temp, out)
        task.wait()
        out_tensor_list.clear()
        out_tensor_list.extend(paddle.split(out, nranks, 0))
        return

    if _non_static_mode():
        out = _C_ops.alltoall(temp, 'use_calc_stream', use_calc_stream,
                              'ring_id', ring_id)
    else:
        op_type = 'alltoall'
        helper = LayerHelper(op_type, **locals())
        out = helper.create_variable_for_type_inference(
            dtype=in_tensor_list[0].dtype)

        if not isinstance(in_tensor_list, list):
            raise ValueError("The type of 'in_tensor_list' for all_to_all "
                             "should be list.")
        for elem in in_tensor_list:
            check_variable_and_dtype(
                elem, 'in_tensor_list',
                ['float16', 'float32', 'float64', 'int32', 'int64'],
                'all_to_all')
        if not isinstance(out_tensor_list, list):
            raise ValueError("The type of 'out_tensor_list' for all_to_all "
                             "should be list.")
        if len(out_tensor_list) != 0:
            raise ValueError("The 'out_tensor_list' for all_to_all "
                             "must be an empty list.")
        helper.append_op(
            type=op_type,
            inputs={'X': [temp]},
            outputs={'Out': [out]},
            attrs={
                'ring_id': ring_id,
                'use_calc_stream': use_calc_stream,
            })
    out_tensor_list.extend(paddle.split(out, nranks, 0))


def send(tensor, dst=0, group=None, use_calc_stream=True):
    """
    Send a tensor to the receiver.

    Args:
        tensor (Tensor): The Tensor to send. Its data type
            should be float16, float32, float64, int32 or int64.
        dst (int): The destination rank id.
        group (Group, optional): The group instance return by new_group or None for global default group. Default: None.
        use_calc_stream (bool, optional): Whether to use calculate stream or communication stream. Default: True.
    
    Returns:
        None.

    Examples:
        .. code-block:: python

            # required: distributed
            import paddle
            from paddle.distributed import init_parallel_env

            init_parallel_env()
            if paddle.distributed.ParallelEnv().rank == 0:
                data = paddle.to_tensor([7, 8, 9])
                paddle.distributed.send(data, dst=1)
            else:
                data = paddle.to_tensor([1,2,3])
                paddle.distributed.recv(data, src=0)
            out = data.numpy()
    """
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        group = _get_default_group() if group is None else group
        task = group.process_group.send(tensor, dst)
        if use_calc_stream:
            task.wait()
            return None
        else:
            return task

    ring_id = 0 if group is None else group.id

    if _non_static_mode():
        return _C_ops.send_v2(tensor, 'use_calc_stream', use_calc_stream,
                              'ring_id', ring_id, 'peer', dst)
    op_type = 'send_v2'
    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        'send')

    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        inputs={'X': [tensor]},
        attrs={
            'ring_id': ring_id,
            'peer': dst,
            'use_calc_stream': use_calc_stream,
        })


def recv(tensor, src=0, group=None, use_calc_stream=True):
    """
    Receive a tensor to the sender.

    Args:
        tensor (Tensor): The Tensor to receive. Its data type
            should be float16, float32, float64, int32 or int64.
        src (int): The source rank id.
        group (Group, optional): The group instance return by new_group or None for global default group. Default: None.
        use_calc_stream (bool, optional): Whether to use calculate stream or communication stream. Default: True.
    
    Returns:
        None.

    Examples:
        .. code-block:: python

            # required: distributed
            import paddle
            from paddle.distributed import init_parallel_env

            init_parallel_env()
            if paddle.distributed.ParallelEnv().rank == 0:
                data = paddle.to_tensor([7, 8, 9])
                paddle.distributed.send(data, dst=1)
            else:
                data = paddle.to_tensor([1,2,3])
                paddle.distributed.recv(data, src=0)
            out = data.numpy()
    """
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        group = _get_default_group() if group is None else group
        task = group.process_group.recv(tensor, src)
        if use_calc_stream:
            task.wait()
            return None
        else:
            return task

    ring_id = 0 if group is None else group.id

    if _non_static_mode():
        return _C_ops.recv_v2(tensor, 'use_calc_stream', use_calc_stream,
                              'ring_id', ring_id, 'peer', src, 'dtype',
                              tensor.dtype, 'out_shape', tensor.shape)
    op_type = 'recv_v2'
    check_variable_and_dtype(
        tensor, 'tensor', ['float16', 'float32', 'float64', 'int32', 'int64'],
        'recv')
    helper = LayerHelper(op_type, **locals())
    helper.append_op(
        type=op_type,
        outputs={'Out': [tensor]},
        attrs={
            'ring_id': ring_id,
            'peer': src,
            'out_shape': tensor.shape,
            'dtype': tensor.dtype,
            'use_calc_stream': use_calc_stream,
        })
