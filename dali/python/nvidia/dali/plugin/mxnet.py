# Copyright (c) 2017-2019, NVIDIA CORPORATION. All rights reserved.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from nvidia.dali.pipeline import Pipeline
from nvidia.dali import types
import mxnet as mx
import ctypes
import logging
import numpy as np

# MXNet currently does not expose WaitToWrite C API call
# in Python API
def _wait_to_write(arr):
    if not isinstance(arr, mx.nd.NDArray):
        raise RuntimeError("Can only wait for NDArray")
    mx.base._LIB.MXNDArrayWaitToWrite(arr.handle)

def feed_ndarray(dali_tensor, arr):
    """
    Copy contents of DALI tensor to MXNet's NDArray.

    Parameters
    ----------
    `dali_tensor` : nvidia.dali.backend.TensorCPU or nvidia.dali.backend.TensorGPU
                    Tensor from which to copy
    `arr` : mxnet.nd.NDArray
            Destination of the copy
    """
    # Wait until arr is no longer used by the engine
    _wait_to_write(arr)
    assert dali_tensor.shape() == list(arr.shape), \
            ("Shapes do not match: DALI tensor has shape {0}"
            ", but NDArray has shape {1}".format(dali_tensor.shape(), list(arr.shape)))
    # Get CTypes void pointer to the underlying memory held by arr
    ptr = ctypes.c_void_p()
    mx.base._LIB.MXNDArrayGetData(arr.handle, ctypes.byref(ptr))
    # Copy data from DALI tensor to ptr
    dali_tensor.copy_to_external(ptr)

class DALIGenericIterator(object):
    """
    General DALI iterator for MXNet. It can return any number of
    outputs from the DALI pipeline in the form of MXNet's DataBatch
    of NDArrays.

    Please keep in mind that NDArrays returned by the iterator are
    still owned by DALI. They are valid till the next iterator call.
    If the content needs to be preserved please copy it to another NDArray.

    Parameters
    ----------
    pipelines : list of nvidia.dali.pipeline.Pipeline
                List of pipelines to use
    output_map : list of (str, str)
                 List of pairs (output_name, tag) which maps consecutive
                 outputs of DALI pipelines to proper field in MXNet's
                 DataBatch.
                 tag is one of DALIGenericIterator.DATA_TAG
                 and DALIGenericIterator.LABEL_TAG mapping given output
                 for data or label correspondingly.
                 output_names should be distinct.
    size : int
          Number of samples in the epoch (Usually the size of the dataset).
    data_layout : str, optional, default = 'NCHW'
                  Either 'NHWC' or 'NCHW' - layout of the pipeline outputs.
    fill_last_batch : bool, optional, default = True
                 Whether to return a fraction of a full batch of data
                 such that the total entries returned by the
                 iterator == 'size'. Setting this flag to False will
                 cause the iterator to return the first integer multiple
                 of self._num_gpus * self.batch_size which exceeds 'size'.
    auto_reset : bool, optional, default = False
                 Whether the iterator resets itself for the next epoch
                 or it requires reset() to be called separately.
    squeeze_labels: bool, optional, default = True
                 Whether the iterator should squeeze the labels before
                 copying them to the ndarray.
    dynamic_shape: bool, optional, default = False
                 Whether the shape of the output of the DALI pipeline can
                 change during execution. If True, the mxnet.ndarray will be resized accordingly
                 if the shape of DALI returned tensors changes during execution.
                 If False, the iterator will fail in case of change.
    last_batch_padded : bool, optional, default = False
                 Whether the last batch provided by DALI is padded with the last sample
                 or it just wraps up. In the conjunction with `fill_last_batch` it tells
                 if the iterator returning last batch with data only partially filled with
                 data from the current epoch is dropping padding samples or samples from
                 the next epoch. If set to False next epoch will end sooner as data from
                 it was consumed but dropped. If set to True next epoch would be the
                 same length as the first one. For this happen, the option `pad_last_batch`
                 in the reader need to be set to `True` as well.

    Example
    -------
    With the data set [1,2,3,4,5,6,7] and the batch size 2:
    fill_last_batch = False, last_batch_padded = True  -> last batch = [7], next iteration will return [1, 2]
    fill_last_batch = False, last_batch_padded = False -> last batch = [7], next iteration will return [2, 3]
    fill_last_batch = True, last_batch_padded = True   -> last batch = [7, 7], next iteration will return [1, 2]
    fill_last_batch = True, last_batch_padded = False  -> last batch = [7, 1], next iteration will return [2, 3]
    """
    def __init__(self,
                 pipelines,
                 output_map,
                 size,
                 data_layout='NCHW',
                 fill_last_batch=True,
                 auto_reset=False,
                 squeeze_labels=True,
                 dynamic_shape=False,
                 last_batch_padded=False):
        if not isinstance(pipelines, list):
            pipelines = [pipelines]
        self._num_gpus = len(pipelines)
        assert pipelines is not None, "Number of provided pipelines has to be at least 1"
        self.batch_size = pipelines[0].batch_size
        self._size = int(size)
        self._pipes = pipelines
        self._fill_last_batch = fill_last_batch
        self._last_batch_padded = last_batch_padded
        self._auto_reset = auto_reset
        self._squeeze_labels = squeeze_labels
        self._dynamic_shape = dynamic_shape
        # Build all pipelines
        for p in self._pipes:
            with p._check_api_type_scope(types.PipelineAPIType.ITERATOR) as check:
                p.build()
        # Use double-buffering of data batches
        self._data_batches = [[None] for i in range(self._num_gpus)]
        self._counter = 0
        self._current_data_batch = 0
        self._output_names_map = [x[0] for x in output_map]
        self._output_categories_map = [x[1] for x in output_map]
        self._output_categories = {DALIGenericIterator.DATA_TAG, DALIGenericIterator.LABEL_TAG}
        assert set(self._output_categories_map) <= self._output_categories, \
            "Only DATA_TAG and LABEL_TAG are allowed"
        assert len(set(self._output_names_map)) == len(self._output_names_map), \
            "output_names in output_map should be distinct"
        self.output_map = output_map

        # We need data about the batches (like shape information),
        # so we need to run a single batch as part of setup to get that info
        for p in self._pipes:
            with p._check_api_type_scope(types.PipelineAPIType.ITERATOR) as check:
                p.schedule_run()
        self._first_batch = None
        self._first_batch = self.next()
        # Set data descriptors for MXNet
        self.provide_data = []
        self.provide_label = []

        category_names = {key : [] for key in self._output_categories}
        for name, category in output_map:
            category_names[category].append(name)
        for i, data in enumerate(self._first_batch[0].data):
            data_shape  = (data.shape[0] * self._num_gpus,) + data.shape[1:]
            self.provide_data.append(mx.io.DataDesc(category_names[DALIGenericIterator.DATA_TAG][i], \
                data_shape, data.dtype, layout=data_layout))
        for i, label in enumerate(self._first_batch[0].label):
            label_shape = (label.shape[0] * self._num_gpus,) + label.shape[1:]
            self.provide_label.append(mx.io.DataDesc(category_names[DALIGenericIterator.LABEL_TAG][i], \
                label_shape, label.dtype))


    def __next__(self):
        if self._first_batch is not None:
            batch = self._first_batch
            self._first_batch = None
            return batch
        if self._counter >= self._size:
            if self._auto_reset:
                self.reset()
            raise StopIteration
        # Gather outputs
        outputs = []
        for p in self._pipes:
            with p._check_api_type_scope(types.PipelineAPIType.ITERATOR) as check:
                outputs.append(p.share_outputs())
        for i in range(self._num_gpus):
            # MXNet wants batches with clear distinction between
            # data and label entries, so segregate outputs into
            # 2 categories
            category_outputs = {key : [] for key in self._output_categories}
            for j, out in enumerate(outputs[i]):
                category_outputs[self._output_categories_map[j]].append(out)
            # Change DALI TensorLists into Tensors
            category_tensors = dict()
            category_info = dict()
            # For data proceed normally
            category_tensors[DALIGenericIterator.DATA_TAG] = \
                [x.as_tensor() for x in category_outputs[DALIGenericIterator.DATA_TAG]]
            category_info[DALIGenericIterator.DATA_TAG] = \
                [(x.shape(), np.dtype(x.dtype())) for x in category_tensors[DALIGenericIterator.DATA_TAG]]
            # For labels we squeeze the tensors
            category_tensors[DALIGenericIterator.LABEL_TAG] = \
                [x.as_tensor() for x in category_outputs[DALIGenericIterator.LABEL_TAG]]
            if self._squeeze_labels:
                for label in category_tensors[DALIGenericIterator.LABEL_TAG]:
                    label.squeeze()
            category_info[DALIGenericIterator.LABEL_TAG] = \
                [(x.shape(), np.dtype(x.dtype())) for x in category_tensors[DALIGenericIterator.LABEL_TAG]]

            # If we did not yet allocate memory for that batch, do it now
            if self._data_batches[i][self._current_data_batch] is None:
                mx_gpu_device = mx.gpu(self._pipes[i].device_id)
                mx_cpu_device = mx.cpu(0)
                from nvidia.dali.backend import TensorGPU
                category_device = {key : [] for key in self._output_categories}
                for category in self._output_categories:
                    for t in category_tensors[category]:
                        if type(t) is TensorGPU:
                            category_device[category].append(mx_gpu_device)
                        else:
                            category_device[category].append(mx_cpu_device)
                d = []
                l = []
                for j, (shape, dtype) in enumerate(category_info[DALIGenericIterator.DATA_TAG]):
                    d.append(mx.nd.zeros(shape, category_device[DALIGenericIterator.DATA_TAG][j], dtype = dtype))
                for j, (shape, dtype) in enumerate(category_info[DALIGenericIterator.LABEL_TAG]):
                    l.append(mx.nd.zeros(shape, category_device[DALIGenericIterator.LABEL_TAG][j], dtype = dtype))

                self._data_batches[i][self._current_data_batch] = mx.io.DataBatch(data=d, label=l)

            d = self._data_batches[i][self._current_data_batch].data
            l = self._data_batches[i][self._current_data_batch].label
            # Copy data from DALI Tensors to MXNet NDArrays
            if self._dynamic_shape:
                for j, (shape, dtype) in enumerate(category_info[DALIGenericIterator.DATA_TAG]):
                    if list(d[j].shape) != shape:
                        d[j] = mx.nd.zeros(shape, d[j].context, dtype = dtype)
                for j, (shape, dtype) in enumerate(category_info[DALIGenericIterator.LABEL_TAG]):
                    if list(l[j].shape) != shape:
                        l[j] = mx.nd.zeros(shape, l[j].context, dtype = dtype)

            for j, d_arr in enumerate(d):
                feed_ndarray(category_tensors[DALIGenericIterator.DATA_TAG][j], d_arr)
            for j, l_arr in enumerate(l):
                feed_ndarray(category_tensors[DALIGenericIterator.LABEL_TAG][j], l_arr)

        for p in self._pipes:
            with p._check_api_type_scope(types.PipelineAPIType.ITERATOR) as check:
                p.release_outputs()
                p.schedule_run()

        copy_db_index = self._current_data_batch
        # Change index for double buffering
        self._current_data_batch = (self._current_data_batch + 1) % 1
        self._counter += self._num_gpus * self.batch_size

        # padding the last batch
        if (not self._fill_last_batch) and (self._counter > self._size):
                # this is the last batch and we need to pad
                overflow = self._counter - self._size
                overflow_per_device = overflow // self._num_gpus
                difference = self._num_gpus - (overflow % self._num_gpus)
                for i in range(self._num_gpus):
                    if i < difference:
                        self._data_batches[i][copy_db_index].pad = overflow_per_device
                    else:
                        self._data_batches[i][copy_db_index].pad = overflow_per_device + 1
        else:
            for db in self._data_batches:
                db[copy_db_index].pad = 0

        return [db[copy_db_index] for db in self._data_batches]

    def next(self):
        """
        Returns the next batch of data.
        """
        return self.__next__()

    def __iter__(self):
        return self

    def reset(self):
        """
        Resets the iterator after the full epoch.
        DALI iterators do not support resetting before the end of the epoch
        and will ignore such request.
        """
        if self._counter >= self._size:
            if self._fill_last_batch and not self._last_batch_padded:
                self._counter = self._counter % self._size
            else:
                self._counter = 0
            for p in self._pipes:
                p.reset()
                if p.empty():
                    with p._check_api_type_scope(types.PipelineAPIType.ITERATOR) as check:
                        p.schedule_run()
        else:
            logging.warning("DALI iterator does not support resetting while epoch is not finished. Ignoring...")

    DATA_TAG = "data"
    LABEL_TAG = "label"

class DALIClassificationIterator(DALIGenericIterator):
    """
    DALI iterator for classification tasks for MXNet. It returns 2 outputs
    (data and label) in the form of MXNet's DataBatch of NDArrays.

    Calling

    .. code-block:: python

       DALIClassificationIterator(pipelines, size, data_name,
                                  label_name, data_layout)

    is equivalent to calling

    .. code-block:: python

       DALIGenericIterator(pipelines,
                           [data_name, DALIClassificationIterator.DATA_TAG,
                            label_name, DALIClassificationIterator.LABEL_TAG],
                           size, data_name, label_name,
                           data_layout)

    Please keep in mind that NDArrays returned by the iterator are
    still owned by DALI. They are valid till the next iterator call.
    If the content needs to be preserved please copy it to another NDArray.

    Parameters
    ----------
    pipelines : list of nvidia.dali.pipeline.Pipeline
                List of pipelines to use
    size : int
           Number of samples in the epoch (Usually the size of the dataset).
    data_name : str, optional, default = 'data'
                Data name for provided symbols.
    label_name : str, optional, default = 'softmax_label'
                 Label name for provided symbols.
    data_layout : str, optional, default = 'NCHW'
                  Either 'NHWC' or 'NCHW' - layout of the pipeline outputs.
    fill_last_batch : bool, optional, default = True
                 Whether to return a fraction of a full batch of data
                 such that the total entries returned by the
                 iterator == 'size'. Setting this flag to False will
                 cause the iterator to return the first integer multiple
                 of self._num_gpus * self.batch_size which exceeds 'size'.
    auto_reset : bool, optional, default = False
                 Whether the iterator resets itself for the next epoch
                 or it requires reset() to be called separately.
    squeeze_labels: bool, optional, default = True
                 Whether the iterator should squeeze the labels before
                 copying them to the ndarray.
    dynamic_shape: bool, optional, default = False
                 Whether the shape of the output of the DALI pipeline can
                 change during execution. If True, the mxnet.ndarray will be resized accordingly
                 if the shape of DALI returned tensors changes during execution.
                 If False, the iterator will fail in case of change.
    last_batch_padded : bool, optional, default = False
                 Whether the last batch provided by DALI is padded with the last sample
                 or it just wraps up. In the conjunction with `fill_last_batch` it tells
                 if the iterator returning last batch with data only partially filled with
                 data from the current epoch is dropping padding samples or samples from
                 the next epoch. If set to False next epoch will end sooner as data from
                 it was consumed but dropped. If set to True next epoch would be the
                 same length as the first one.

    Example
    -------
    With the data set [1,2,3,4,5,6,7] and the batch size 2:
    fill_last_batch = False, last_batch_padded = True  -> last batch = [7], next iteration will return [1, 2]
    fill_last_batch = False, last_batch_padded = False -> last batch = [7], next iteration will return [2, 3]
    fill_last_batch = True, last_batch_padded = True   -> last batch = [7, 7], next iteration will return [1, 2]
    fill_last_batch = True, last_batch_padded = False  -> last batch = [7, 1], next iteration will return [2, 3]
    """
    def __init__(self,
                 pipelines,
                 size,
                 data_name='data',
                 label_name='softmax_label',
                 data_layout='NCHW',
                 fill_last_batch=True,
                 auto_reset=False,
                 squeeze_labels=True,
                 dynamic_shape=False,
                 last_batch_padded=False):
        super(DALIClassificationIterator, self).__init__(pipelines,
                                                         [(data_name, DALIClassificationIterator.DATA_TAG),
                                                          (label_name, DALIClassificationIterator.LABEL_TAG)],
                                                         size,
                                                         data_layout     = data_layout,
                                                         fill_last_batch = fill_last_batch,
                                                         auto_reset = auto_reset,
                                                         squeeze_labels=squeeze_labels,
                                                         dynamic_shape=dynamic_shape,
                                                         last_batch_padded = last_batch_padded)
