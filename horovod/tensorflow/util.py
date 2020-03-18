# Copyright 2019 Uber Technologies, Inc. All Rights Reserved.
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
# ==============================================================================

import io

from distutils.version import LooseVersion

import cloudpickle

import tensorflow as tf

from tensorflow.python.framework import ops

from horovod.tensorflow.mpi_ops import broadcast
from horovod.tensorflow.mpi_ops import rank


if LooseVersion(tf.__version__) >= LooseVersion('1.7.0'):  # Eager Mode has been introduced in TF 1.7.0
    from tensorflow.python.eager import context
    _has_eager = True
else:
    _has_eager = False


def _executing_eagerly():
    """Returns true if eager execution is supported and enabled."""
    return _has_eager and context.executing_eagerly()


def _make_subgraph(f):
    if hasattr(tf, 'function'):
        # TensorFlow 1.14.0+
        return tf.function(f)
    else:
        return tf.contrib.eager.defun(f)


def _cache(f):
    cache = dict()

    def wrapper(*args):
        key = (args, _executing_eagerly())

        if key in cache:
            return cache[key]
        else:
            retval = f(*args)
            cache[key] = retval
            return retval

    return wrapper


@_cache
def _make_broadcast_group_fn():
    if _executing_eagerly():
        # Eager mode will parallelize independent control flow
        def broadcast_group(variables, root_rank):
            for var in variables:
                var.assign(broadcast(var, root_rank))

        return _make_subgraph(broadcast_group)
    else:
        # Graph mode requires an Op
        def broadcast_group(variables, root_rank):
            return tf.group(*[var.assign(broadcast(var, root_rank))
                              for var in variables])

        return broadcast_group


def broadcast_variables(variables, root_rank):
    """Broadcasts variables from root rank to all other processes.

    Arguments:
        variables: variables for broadcast
        root_rank: rank of the process from which global variables will be broadcasted
                   to all other processes.
    """
    broadcast_group = _make_broadcast_group_fn()
    return broadcast_group(variables, root_rank)


def broadcast_object(obj, root_rank, session=None, name=None):
    """
    Serializes and broadcasts an object from root rank to all other processes.

    Arguments:
        obj: An object capable of being serialized without losing any context.
        root_rank: The rank of the process from which parameters will be
                   broadcasted to all other processes.
        session: Session for TensorFlow v1 compatibility.
        name: Optional name to use during broadcast, will default to the class
              type.
    Returns:
        The object that was broadcast from the `root_rank`.
    """
    if name is None:
        name = type(obj).__name__

    def to_numpy(v):
        if not _executing_eagerly():
            sess = session or ops.get_default_session()
            return sess.run(v)
        else:
            return v.numpy()

    if rank() == root_rank:
        b = io.BytesIO()
        cloudpickle.dump(obj, b)
        t = tf.convert_to_tensor(bytearray(b.getvalue()), dtype=tf.uint8)
        sz = tf.convert_to_tensor([t.shape[0]], dtype=tf.int32)
        to_numpy(broadcast(sz, root_rank, name + '.sz'))
    else:
        sz = tf.convert_to_tensor([0], dtype=tf.int32)
        sz = to_numpy(broadcast(sz, root_rank, name + '.sz'))
        t = tf.zeros(sz.tolist()[0], dtype=tf.uint8)

    t = to_numpy(broadcast(t, root_rank, name + '.t'))

    if rank() != root_rank:
        buf = io.BytesIO(t.tobytes())
        obj = cloudpickle.load(buf)

    return obj
