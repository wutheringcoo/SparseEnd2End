# Copyright (c) 2024 SparseEnd2End. All rights reserved @author: Thomas Von Wu.
import os
import time
import copy
import logging
import argparse

import onnx
from onnxsim import simplify

import torch
from torch import nn

from modules.sparse4d_detector import *
from modules.head.sparse4d_blocks.instance_bank import topk

from tool.utils.config import read_cfg
from typing import Optional, Dict, Any

from tool.utils.logger import set_logger


def parse_args():
    parser = argparse.ArgumentParser(description="Deploy PerceptionE2E Head!")
    parser.add_argument(
        "--cfg",
        type=str,
        default="dataset/config/sparse4d_temporal_r50_1x1_bs1_256x704_mini.py",
        help="deploy config file path",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="ckpt/sparse4dv3_r50.pth",
        help="deploy ckpt path",
    )
    parser.add_argument(
        "--log",
        type=str,
        default="deploy/onnxlog/export_head_onnx.log",
    )
    parser.add_argument(
        "--save_onnx1",
        type=str,
        default="deploy/onnxlog/sparse4dhead1st_frame.onnx",
    )
    parser.add_argument(
        "--save_onnx2",
        type=str,
        default="deploy/onnxlog/sparse4dhead2rd.onnx",
    )
    parser.add_argument(
        "--osec", action="store_true", help="only export sparse4dhead2rd onnx."
    )
    args = parser.parse_args()
    return args


class Sparse4DHead1st(nn.Module):
    def __init__(self, model):
        super(Sparse4DHead1st, self).__init__()
        self.model = model

    @staticmethod
    def head_forward(
        self,
        instance_feature,
        anchor,
        time_interval,
        feature,
        spatial_shapes,
        level_start_index,
        lidar2img,
        image_wh,
    ):

        # instance bank get inputs
        temp_instance_feature = None
        temp_anchor_embed = None

        # DAF inputs
        metas = {
            "lidar2img": lidar2img,
            "image_wh": image_wh,
        }

        anchor_embed = self.anchor_encoder(anchor)

        feature_maps = [feature, spatial_shapes, level_start_index]
        prediction = []
        for i, op in enumerate(self.operation_order):
            print("i: ", i, "\top: ", op)
            if self.layers[i] is None:
                continue
            elif op == "temp_gnn":
                instance_feature = self.graph_model(
                    i,
                    instance_feature,
                    temp_instance_feature,
                    temp_instance_feature,
                    query_pos=anchor_embed,
                    key_pos=temp_anchor_embed,
                )
            elif op == "gnn":
                instance_feature = self.graph_model(
                    i,
                    instance_feature,
                    value=instance_feature,
                    query_pos=anchor_embed,
                )
            elif op == "norm" or op == "ffn":
                instance_feature = self.layers[i](instance_feature)
            elif op == "deformable":
                instance_feature = self.layers[i](
                    instance_feature,
                    anchor,
                    anchor_embed,
                    feature_maps,
                    metas,
                )
            elif op == "refine":
                anchor, cls, qt = self.layers[i](
                    instance_feature,
                    anchor,
                    anchor_embed,
                    time_interval=time_interval,
                    return_cls=(
                        len(prediction) == self.num_single_frame_decoder - 1
                        or i == len(self.operation_order) - 1
                    ),
                )
                prediction.append(anchor)
                if i != len(self.operation_order) - 1:
                    anchor_embed = self.anchor_encoder(anchor)
        return instance_feature, anchor, cls, qt

    def forward(
        self,
        instance_feature,
        anchor,
        time_interval,
        feature,
        spatial_shapes,
        level_start_index,
        lidar2img,
        image_wh,
    ):
        head = self.model.head
        instance_feature, anchor, cls, qt = self.head_forward(
            head,
            instance_feature,
            anchor,
            time_interval,
            feature,
            spatial_shapes,
            level_start_index,
            lidar2img,
            image_wh,
        )
        return instance_feature, anchor, cls, qt


class Sparse4DHead2rd(nn.Module):
    def __init__(self, model):
        super(Sparse4DHead2rd, self).__init__()
        self.model = model

    @staticmethod
    def head_forward(
        self,
        temp_instance_feature,
        temp_anchor,
        mask,
        track_id,
        instance_feature,
        anchor,
        time_interval,
        feature,
        spatial_shapes,
        level_start_index,
        lidar2img,
        image_wh,
    ):
        anchor_embed = self.anchor_encoder(anchor)
        temp_anchor_embed = self.anchor_encoder(temp_anchor)

        # DAF inputs
        metas = {
            "lidar2img": lidar2img,
            "image_wh": image_wh,
        }

        feature_maps = [feature, spatial_shapes, level_start_index]
        prediction = []
        for i, op in enumerate(self.operation_order):
            print("op:  ", op)
            if self.layers[i] is None:
                continue
            elif op == "temp_gnn":
                instance_feature = self.graph_model(
                    i,
                    instance_feature,
                    temp_instance_feature,
                    temp_instance_feature,
                    query_pos=anchor_embed,
                    key_pos=temp_anchor_embed,
                )
            elif op == "gnn":
                instance_feature = self.graph_model(
                    i,
                    instance_feature,
                    value=instance_feature,
                    query_pos=anchor_embed,
                )
            elif op == "norm" or op == "ffn":
                instance_feature = self.layers[i](instance_feature)
            elif op == "deformable":
                instance_feature = self.layers[i](
                    instance_feature,
                    anchor,
                    anchor_embed,
                    feature_maps,
                    metas,
                )
            elif op == "refine":
                anchor, cls, qt = self.layers[i](
                    instance_feature,
                    anchor,
                    anchor_embed,
                    time_interval=time_interval,
                    return_cls=(
                        len(prediction) == self.num_single_frame_decoder - 1
                        or i == len(self.operation_order) - 1
                    ),
                )
                prediction.append(anchor)

                # update in head refine
                if len(prediction) == self.num_single_frame_decoder:
                    N = (
                        self.instance_bank.num_anchor
                        - self.instance_bank.num_temp_instances
                    )
                    cls = cls.max(dim=-1).values
                    _, (selected_feature, selected_anchor) = topk(
                        cls, N, instance_feature, anchor
                    )
                    selected_feature = torch.cat(
                        [temp_instance_feature, selected_feature], dim=1
                    )
                    selected_anchor = torch.cat([temp_anchor, selected_anchor], dim=1)
                    instance_feature = torch.where(
                        mask[:, None, None], selected_feature, instance_feature
                    )
                    anchor = torch.where(mask[:, None, None], selected_anchor, anchor)
                    track_id = torch.where(
                        mask[:, None],
                        track_id,
                        track_id.new_tensor(-1),
                    )

                if i != len(self.operation_order) - 1:
                    anchor_embed = self.anchor_encoder(anchor)
                if len(prediction) > self.num_single_frame_decoder:
                    temp_anchor_embed = anchor_embed[
                        :, : self.instance_bank.num_temp_instances
                    ]
        return instance_feature, anchor, cls, qt, track_id

    def forward(
        self,
        temp_instance_feature,
        temp_anchor,
        mask,
        track_id,
        instance_feature,
        anchor,
        time_interval,
        feature,
        spatial_shapes,
        level_start_index,
        lidar2img,
        image_wh,
    ):
        head = self.model.head
        instance_feature, anchor, cls, qt, track_id = self.head_forward(
            head,
            temp_instance_feature,
            temp_anchor,
            mask,
            track_id,
            instance_feature,
            anchor,
            time_interval,
            feature,
            spatial_shapes,
            level_start_index,
            lidar2img,
            image_wh,
        )
        return instance_feature, anchor, cls, qt, track_id


def dummpy_input(
    model,
    bs: int,
    nums_cam: int,
    input_h: int,
    input_w: int,
    nums_query=900,
    nums_topk=600,
    embed_dims=256,
    anchor_dims=11,
    first_frame=True,
    logger=None,
):
    """
    Return:
        dummy_level_start_index: torch.int32
    """
    instance_feature = model.head.instance_bank.instance_feature  # (900, 256)
    dummy_instance_feature = (
        instance_feature[None].repeat((bs, 1, 1)).cuda()
    )  # (bs, 900, 256)

    anchor = model.head.instance_bank.anchor  # (900, 11)
    dummy_anchor = anchor[None].repeat((bs, 1, 1)).cuda()  # (bs, 900, 11)

    dummy_temp_instance_feature = (
        torch.zeros((bs, nums_topk, embed_dims)).float().cuda()
    )
    dummy_temp_anchor = torch.zeros((bs, nums_topk, anchor_dims)).float().cuda()
    dummy_mask = torch.randint(0, 2, size=(bs,)).bool().cuda()
    dummy_track_id = -1 * torch.ones((bs, nums_query)).int().cuda()

    dummy_time_interval = torch.tensor(
        [model.head.instance_bank.default_time_interval] * bs
    ).cuda()

    h_4x, w_4x = input_h // 4, input_w // 4
    h_8x, w_8x = input_h // 8, input_w // 8
    h_16x, w_16x = input_h // 16, input_w // 16
    h_32x, w_32x = input_h // 32, input_w // 32
    feature_size = nums_cam * (
        h_4x * w_4x + h_8x * w_8x + h_16x * w_16x + h_32x * w_32x
    )
    dummy_feature = torch.randn(bs, feature_size, embed_dims).float().cuda()

    dummy_spatial_shapes = (
        torch.tensor([[h_4x, w_4x], [h_8x, w_8x], [h_16x, w_16x], [h_32x, w_32x]])
        .int()
        .unsqueeze(0)
        .repeat(nums_cam, 1, 1)
        .cuda()
    )

    scale_start_index = dummy_spatial_shapes[..., 0] * dummy_spatial_shapes[..., 1]
    scale_start_index = scale_start_index.flatten().cumsum(dim=0).int()
    scale_start_index = torch.cat(
        [torch.tensor([0]).to(scale_start_index), scale_start_index[:-1]]
    )
    dummy_level_start_index = scale_start_index.reshape(nums_cam, 4)

    dummy_lidar2img = torch.randn(bs, nums_cam, 4, 4).to(dummy_feature)
    dummy_image_wh = (
        torch.tensor([input_w, input_h])
        .unsqueeze(0)
        .unsqueeze(0)
        .repeat(bs, nums_cam, 1)
        .to(dummy_feature)
    )

    logger.debug(f"Dummy input : hape&Type&Device Msg >>>>>>")
    roi_x = [
        "dummy_instance_feature",
        "dummy_anchor",
        "dummy_time_interval",
        "dummy_feature",
        "dummy_spatial_shapes",
        "dummy_level_start_index",
        "dummy_image_wh",
        "dummy_lidar2img",
    ]
    for x in roi_x:
        logger.debug(
            f"{x}\t:\tshape={eval(x).shape},\tdtype={eval(x).dtype},\tdevice={eval(x).device}"
        )

    if first_frame:
        logger.debug(f"Frame > 1: Extra dummy input is needed >>>>>>>")
        roi_y = [
            "dummy_temp_instance_feature",
            "dummy_temp_anchor",
            "dummy_mask",
            "dummy_track_id",
        ]
        for y in roi_y:
            logger.debug(
                f"{y}\t:\tshape={eval(y).shape},\tdtype={eval(y).dtype},\tdevice={eval(y).device}"
            )

    return (
        dummy_instance_feature,
        dummy_anchor,
        dummy_time_interval,
        dummy_feature,
        dummy_spatial_shapes,
        dummy_level_start_index,
        dummy_lidar2img,
        dummy_image_wh,
        dummy_temp_instance_feature,
        dummy_temp_anchor,
        dummy_mask,
        dummy_track_id,
    )


def build_module(cfg, default_args: Optional[Dict] = None) -> Any:
    cfg2 = cfg.copy()
    if default_args is not None:
        for name, value in default_args.items():
            cfg2.setdefault(name, value)
    type = cfg2.pop("type")
    return eval(type)(**cfg2)


if __name__ == "__main__":
    args = parse_args()
    os.makedirs(os.path.dirname(args.save_onnx1), exist_ok=True)
    logger, console_handler, file_handler = set_logger(args.log, True)
    logger.setLevel(logging.DEBUG)
    console_handler.setLevel(logging.DEBUG)
    file_handler.setLevel(logging.DEBUG)

    cfg = read_cfg(args.cfg)
    model = build_module(cfg["model"])
    checkpoint = args.ckpt
    _ = model.load_state_dict(torch.load(checkpoint)["state_dict"], strict=False)
    model.eval()

    BS = 1
    NUMS_CAM = 6
    INPUT_H = 256
    INPUT_W = 704
    first_frame = True
    (
        dummy_instance_feature,
        dummy_anchor,
        dummy_time_interval,
        dummy_feature,
        dummy_spatial_shapes,
        dummy_level_start_index,
        dummy_lidar2img,
        dummy_image_wh,
        dummy_temp_instance_feature,
        dummy_temp_anchor,
        dummy_mask,
        dummy_track_id,
    ) = dummpy_input(
        model, BS, NUMS_CAM, INPUT_H, INPUT_W, first_frame=first_frame, logger=logger
    )

    if not args.osec:
        first_frame_head = Sparse4DHead1st(copy.deepcopy(model)).cuda()
        logger.info("Export Sparse4DHead1st Onnx >>>>>>>>>>>>>>>>")
        time.sleep(2)
        with torch.no_grad():
            torch.onnx.export(
                first_frame_head,
                (
                    dummy_instance_feature,
                    dummy_anchor,
                    dummy_time_interval,
                    dummy_feature,
                    dummy_spatial_shapes,
                    dummy_level_start_index,
                    dummy_lidar2img,
                    dummy_image_wh,
                ),
                args.save_onnx1,
                input_names=[
                    "instance_feature",
                    "anchor",
                    "time_interval",
                    "feature",
                    "spatial_shapes",
                    "level_start_index",
                    "lidar2img",
                    "image_wh",
                ],
                output_names=[
                    "instance_feature",
                    "anchor",
                    "class_score",
                    "quality_score",
                ],
                opset_version=15,
                do_constant_folding=True,
                verbose=False,
            )

            onnx_orig = onnx.load(args.save_onnx1)
            onnx_simp, check = simplify(onnx_orig)
            assert check, "Simplified ONNX model could not be validated"
            onnx.save(onnx_simp, args.save_onnx1)
            logger.info(
                f'🚀 Export onnx completed. ONNX saved in "{args.save_onnx1}" 🤗.'
            )

    head = Sparse4DHead2rd(copy.deepcopy(model)).cuda()
    logger.info("Export Sparse4DHead2rd Onnx >>>>>>>>>>>>>>>>")
    time.sleep(2)
    with torch.no_grad():
        torch.onnx.export(
            head,
            (
                dummy_temp_instance_feature,
                dummy_temp_anchor,
                dummy_mask,
                dummy_track_id,
                dummy_instance_feature,
                dummy_anchor,
                dummy_time_interval,
                dummy_feature,
                dummy_spatial_shapes,
                dummy_level_start_index,
                dummy_lidar2img,
                dummy_image_wh,
            ),
            args.save_onnx2,
            input_names=[
                "temp_instance_feature",
                "temp_anchor",
                "mask",
                "track_id",
                "instance_feature",
                "anchor",
                "time_interval",
                "feature",
                "spatial_shapes",
                "level_start_index",
                "lidar2img",
                "image_wh",
            ],
            output_names=[
                "instance_feature",
                "anchor",
                "class_score",
                "quality_score",
                "track_id",
            ],
            opset_version=15,
            do_constant_folding=True,
            verbose=False,
        )

        onnx_orig = onnx.load(args.save_onnx2)
        onnx_simp, check = simplify(onnx_orig)
        assert check, "Simplified ONNX model could not be validated!"
        onnx.save(onnx_simp, args.save_onnx2)
        logger.info(f'🚀 Export onnx completed. ONNX saved in "{args.save_onnx2}" 🤗.')
