# Copyright (c) 2024 SparseEnd2End. All rights reserved @author: Thomas Von Wu.
from itertools import chain
from typing import List, Tuple

from torch.nn.parallel import DataParallel
from dataset.utils.scatter_gather import ScatterInputs, scatter_kwargs


class E2EDataParallel(DataParallel):
    """The DataParallel module that supports DataContainer.

    E2EDataParallel has two main differences with PyTorch DataParallel:

    - It supports a custom type :class:`DataContainer` which allows more
      flexible control of input data during both GPU and CPU inference.
    - It implements two more APIs ``train_step()`` and ``val_step()``.

    .. warning::
        E2EDataParallel only supports single GPU training, if you need to
        train with multiple GPUs, please use E2EDistributedDataParallel
        instead. If you have multiple GPUs and you just want to use
        E2EDataParallel, you can set the environment variable
        ``CUDA_VISIBLE_DEVICES=0`` or instantiate ``E2EDataParallel`` with
        ``device_ids=[0]``.

    Args:
        module (:class:`nn.Module`): Module to be encapsulated.
        device_ids (list[int]): Device IDS of modules to be scattered to.
            Defaults to None when GPU is not available.
        output_device (str | int): Device ID for output. Defaults to None.
        dim (int): Dimension used to scatter the data. Defaults to 0.
    """

    def __init__(self, *args, dim: int = 0, **kwargs):
        super().__init__(*args, dim=dim, **kwargs)
        self.dim = dim

    def forward(self, *inputs, **kwargs):
        """Override the original forward function.

        The main difference lies in the CPU inference where the data in
        :class:`DataContainers` will still be gathered.
        """
        if not self.device_ids:
            # We add the following line thus the module could gather and
            # convert data containers as those in GPU inference
            inputs, kwargs = self.scatter(inputs, kwargs, [-1])
            return self.module(*inputs[0], **kwargs[0])
        else:
            return super().forward(*inputs, **kwargs)

    def scatter(
        self, inputs: ScatterInputs, kwargs: ScatterInputs, device_ids: List[int]
    ) -> Tuple[tuple, tuple]:
        return scatter_kwargs(inputs, kwargs, device_ids, dim=self.dim)

    def train_step(self, *inputs, **kwargs):
        if not self.device_ids:
            # We add the following line thus the module could gather and
            # convert data containers as those in GPU inference
            inputs, kwargs = self.scatter(inputs, kwargs, [-1])
            return self.module.train_step(*inputs[0], **kwargs[0])

        assert len(self.device_ids) == 1, (
            "E2EDataParallel only supports single GPU training, if you need to"
            " train with multiple GPUs, please use E2EDistributedDataParallel"
            " instead."
        )

        for t in chain(self.module.parameters(), self.module.buffers()):
            if t.device != self.src_device_obj:
                raise RuntimeError(
                    "module must have its parameters and buffers "
                    f"on device {self.src_device_obj} (device_ids[0]) but "
                    f"found one of them on device: {t.device}"
                )

        inputs, kwargs = self.scatter(inputs, kwargs, self.device_ids)
        return self.module.train_step(*inputs[0], **kwargs[0])

    def val_step(self, *inputs, **kwargs):
        if not self.device_ids:
            # We add the following line thus the module could gather and
            # convert data containers as those in GPU inference
            inputs, kwargs = self.scatter(inputs, kwargs, [-1])
            return self.module.val_step(*inputs[0], **kwargs[0])

        assert len(self.device_ids) == 1, (
            "E2EDataParallel only supports single GPU training, if you need to"
            " train with multiple GPUs, please use E2EDistributedDataParallel"
            " instead."
        )

        for t in chain(self.module.parameters(), self.module.buffers()):
            if t.device != self.src_device_obj:
                raise RuntimeError(
                    "module must have its parameters and buffers "
                    f"on device {self.src_device_obj} (device_ids[0]) but "
                    f"found one of them on device: {t.device}"
                )

        inputs, kwargs = self.scatter(inputs, kwargs, self.device_ids)
        return self.module.val_step(*inputs[0], **kwargs[0])