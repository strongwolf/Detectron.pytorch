import os
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F

from core.config import cfg
import nn as mynn
import utils.net as net_utils
from utils.resnet_weights_helper import convert_state_dict

# ---------------------------------------------------------------------------- #
# Bits for specific architectures (ResNet50, ResNet101, ...)
# ---------------------------------------------------------------------------- #

def ResNet50_conv4_body():
    return ResNet_convX_body((3, 4, 6))


def ResNet50_conv5_body():
    return ResNet_convX_body((3, 4, 6, 3))


def ResNet101_conv4_body():
    return ResNet_convX_body((3, 4, 23))


def ResNet101_conv5_body():
    return ResNet_convX_body((3, 4, 23, 3))


def ResNet152_conv5_body():
    return ResNet_convX_body((3, 8, 36, 3))


# ---------------------------------------------------------------------------- #
# Generic ResNet components
# ---------------------------------------------------------------------------- #


class ResNet_convX_body(nn.Module):
    def __init__(self, block_counts):
        super().__init__()
        self.block_counts = block_counts
        self.convX = len(block_counts) + 1
        self.num_layers = (sum(block_counts) + 3 * (self.convX == 4)) * 3 + 2

        self.res1 = globals()[cfg.RESNETS.STEM_FUNC]()
        dim_in = 64
        dim_bottleneck = cfg.RESNETS.NUM_GROUPS * cfg.RESNETS.WIDTH_PER_GROUP
        self.res2, dim_in = add_stage(dim_in, dim_bottleneck, block_counts[0])
        self.res3, dim_in = add_stage(dim_in, dim_bottleneck * 2, block_counts[1], stride=2)
        self.res4, dim_in = add_stage(dim_in, dim_bottleneck * 4, block_counts[2], stride=2)
        if len(block_counts) == 4:
            if cfg.RESNETS.RES5_DILATION != 1:
                stride = 1
            else:
                stride = 2
            self.res5, dim_in = add_stage(dim_in, dim_bottleneck * 8, block_counts[3],
                                          stride, cfg.RESNETS.RES5_DILATION)
            self.spatial_scale = 1 / 32 * cfg.RESNETS.RES5_DILATION
        else:
            self.spatial_scale = 1 / 16  # final feature scale wrt. original image scale

        self.dim_out = dim_in

        self._init_modules()

    def _init_modules(self):
        assert cfg.RESNETS.FREEZE_AT in [0, 2, 3, 4, 5]
        assert cfg.RESNETS.FREEZE_AT <= self.convX
        for i in range(1, cfg.RESNETS.FREEZE_AT + 1):
            freeze_params(getattr(self, 'res%d' % i))

        # Freeze all bn (affine) layers !!!
        self.apply(lambda m: freeze_params(m) if isinstance(m, mynn.AffineChannel2d) else None)

    def detectron_weight_mapping(self):
        if cfg.RESNETS.USE_GN:
            mapping_to_detectron = {
                'res1.conv1.weight': 'conv1_w',
                'res1.gn1.weight': 'conv1_gn_s',
                'res1.gn1.bias': 'conv1_gn_b',
            }
            orphan_in_detectron = ['pred_w', 'pred_b']
        else:
            mapping_to_detectron = {
                'res1.conv1.weight': 'conv1_w',
                'res1.bn1.weight': 'res_conv1_bn_s',
                'res1.bn1.bias': 'res_conv1_bn_b',
            }
            orphan_in_detectron = ['conv1_b', 'fc1000_w', 'fc1000_b']

        for res_id in range(2, self.convX + 1):
            stage_name = 'res%d' % res_id
            mapping, orphans = residual_stage_detectron_mapping(
                getattr(self, stage_name), stage_name,
                self.block_counts[res_id - 2], res_id)
            mapping_to_detectron.update(mapping)
            orphan_in_detectron.extend(orphans)

        return mapping_to_detectron, orphan_in_detectron

    def train(self, mode=True):
        # Override
        self.training = mode

        for i in range(cfg.RESNETS.FREEZE_AT + 1, self.convX + 1):
            getattr(self, 'res%d' % i).train(mode)

    def forward(self, x):
        for i in range(self.convX):
            x = getattr(self, 'res%d' % (i + 1))(x)
        return x


class ResNet_roi_conv5_head(nn.Module):
    def __init__(self, dim_in, roi_xform_func, spatial_scale):
        super().__init__()
        self.roi_xform = roi_xform_func
        self.spatial_scale = spatial_scale

        dim_bottleneck = cfg.RESNETS.NUM_GROUPS * cfg.RESNETS.WIDTH_PER_GROUP
        stride_init = cfg.FAST_RCNN.ROI_XFORM_RESOLUTION // 7
        self.res5, self.dim_out = add_stage(dim_in, dim_bottleneck * 8, 3, stride_init)
        assert self.dim_out == 2048
        self.avgpool = nn.AvgPool2d(7)

        self._init_modules()

    def _init_modules(self):
        # Freeze all bn (affine) layers !!!
        self.apply(lambda m: freeze_params(m) if isinstance(m, mynn.AffineChannel2d) else None)

    def detectron_weight_mapping(self):
        mapping_to_detectron, orphan_in_detectron = \
          residual_stage_detectron_mapping(self.res5, 'res5', 3, 5)
        return mapping_to_detectron, orphan_in_detectron

    def forward(self, x, rpn_ret):
        x = self.roi_xform(
            x, rpn_ret,
            blob_rois='rois',
            method=cfg.FAST_RCNN.ROI_XFORM_METHOD,
            resolution=cfg.FAST_RCNN.ROI_XFORM_RESOLUTION,
            spatial_scale=self.spatial_scale,
            sampling_ratio=cfg.FAST_RCNN.ROI_XFORM_SAMPLING_RATIO
        )
        res5_feat = self.res5(x)
        x = self.avgpool(res5_feat)
        if cfg.MODEL.SHARE_RES5 and self.training:
            return x, res5_feat
        else:
            return x


def add_stage(inplanes, planes, nblocks, stride=1, dilation=1):
    """Make a stage consist of `nblocks` residual blocks.
    Returns:
        - stage module: an nn.Sequentail module of residual blocks
        - final output dimension
    """
    block_func = globals()[cfg.RESNETS.TRANS_FUNC]
    downsample = None
    if stride != 1 or inplanes != planes * block_func.expansion:
        downsample = globals()[cfg.RESNETS.SHORTCUT_FUNC](
            inplanes, planes * block_func.expansion, stride)

    layers = []
    layers.append(
        block_func(inplanes, planes, stride, dilation=dilation,
                   group=cfg.RESNETS.NUM_GROUPS,
                   downsample=downsample)
    )
    inplanes = planes * block_func.expansion
    for i in range(1, nblocks):
        layers.append(block_func(inplanes, planes, dilation=dilation,
                                 group=cfg.RESNETS.NUM_GROUPS))

    return nn.Sequential(*layers), inplanes


# ------------------------------------------------------------------------------
# various downsample shortcuts (may expand and may consider a new helper)
# ------------------------------------------------------------------------------

def basic_bn_shortcut(inplanes, outplanes, stride):
    return nn.Sequential(
        nn.Conv2d(inplanes,
                  outplanes,
                  kernel_size=1,
                  stride=stride,
                  bias=False),
        mynn.AffineChannel2d(outplanes),
    )


def basic_gn_shortcut(inplanes, outplanes, stride):
    return nn.Sequential(
        nn.Conv2d(inplanes,
                  outplanes,
                  kernel_size=1,
                  stride=stride,
                  bias=False),
        nn.GroupNorm(net_utils.get_group_gn(outplanes), outplanes,
                     eps=cfg.GROUP_NORM.EPSILON)
    )


# ------------------------------------------------------------------------------
# various stems (may expand and may consider a new helper)
# ------------------------------------------------------------------------------

def basic_bn_stem():
    return nn.Sequential(OrderedDict([
        ('conv1', nn.Conv2d(3, 64, 7, stride=2, padding=3, bias=False)),
        ('bn1', mynn.AffineChannel2d(64)),
        ('relu', nn.ReLU(inplace=True)),
        # ('maxpool', nn.MaxPool2d(kernel_size=3, stride=2, padding=0, ceil_mode=True))]))
        ('maxpool', nn.MaxPool2d(kernel_size=3, stride=2, padding=1))]))


def basic_gn_stem():
    return nn.Sequential(OrderedDict([
        ('conv1', nn.Conv2d(3, 64, 7, stride=2, padding=3, bias=False)),
        ('gn1', nn.GroupNorm(net_utils.get_group_gn(64), 64,
                             eps=cfg.GROUP_NORM.EPSILON)),
        ('relu', nn.ReLU(inplace=True)),
        ('maxpool', nn.MaxPool2d(kernel_size=3, stride=2, padding=1))]))


# ------------------------------------------------------------------------------
# various transformations (may expand and may consider a new helper)
# ------------------------------------------------------------------------------

class bottleneck_transformation(nn.Module):
    """ Bottleneck Residual Block """
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, dilation=1, group=1,
                 downsample=None):
        super().__init__()
        # In original resnet, stride=2 is on 1x1.
        # In fb.torch resnet, stride=2 is on 3x3.
        (str1x1, str3x3) = (stride, 1) if cfg.RESNETS.STRIDE_1X1 else (1, stride)

        self.conv1 = nn.Conv2d(
            inplanes, planes, kernel_size=1, stride=str1x1, bias=False)
        self.bn1 = mynn.AffineChannel2d(planes)
        self.conv2 = nn.Conv2d(
            planes,
            planes,
            kernel_size=3,
            stride=str3x3,
            padding=1 * dilation,
            dilation=dilation,
            groups=group,
            bias=False)
        self.bn2 = mynn.AffineChannel2d(planes)
        self.conv3 = nn.Conv2d(
            planes, planes * 4, kernel_size=1, stride=1, bias=False)
        self.bn3 = mynn.AffineChannel2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class bottleneck_gn_transformation(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, dilation=1, group=1,
                 downsample=None):
        super().__init__()
        # In original resnet, stride=2 is on 1x1.
        # In fb.torch resnet, stride=2 is on 3x3.
        (str1x1, str3x3) = (stride, 1) if cfg.RESNETS.STRIDE_1X1 else (1, stride)
        
        self.conv1 = nn.Conv2d(
            inplanes, planes, kernel_size=1, stride=str1x1, bias=False)
        self.gn1 = nn.GroupNorm(net_utils.get_group_gn(planes), planes,
                                eps=cfg.GROUP_NORM.EPSILON)
        self.conv2 = nn.Conv2d(
            planes,
            planes,
            kernel_size=3,
            stride=str3x3,
            padding=1 * dilation,
            dilation=dilation,
            groups=group,
            bias=False)
        self.gn2 = nn.GroupNorm(net_utils.get_group_gn(planes), planes,
                                eps=cfg.GROUP_NORM.EPSILON)
        outplanes = planes * self.expansion
        self.conv3 = nn.Conv2d(
            planes, outplanes, kernel_size=1, stride=1, bias=False)

        self.gn3 = nn.GroupNorm(net_utils.get_group_gn(outplanes), outplanes,
                                eps=cfg.GROUP_NORM.EPSILON)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.gn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.gn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.gn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


# ---------------------------------------------------------------------------- #
# Helper functions
# ---------------------------------------------------------------------------- #

def residual_stage_detectron_mapping(module_ref, module_name, num_blocks, res_id):
    """Construct weight mapping relation for a residual stage with `num_blocks` of
    residual blocks given the stage id: `res_id`
    """
    if cfg.RESNETS.USE_GN:
        norm_suffix = '_gn'
    else:
        norm_suffix = '_bn'
    mapping_to_detectron = {}
    orphan_in_detectron = []
    for blk_id in range(num_blocks):
        detectron_prefix = 'res%d_%d' % (res_id, blk_id)
        my_prefix = '%s.%d' % (module_name, blk_id)

        # residual branch (if downsample is not None)
        if getattr(module_ref[blk_id], 'downsample'):
            dtt_bp = detectron_prefix + '_branch1'  # short for "detectron_branch_prefix"
            mapping_to_detectron[my_prefix
                                 + '.downsample.0.weight'] = dtt_bp + '_w'
            orphan_in_detectron.append(dtt_bp + '_b')
            mapping_to_detectron[my_prefix
                                 + '.downsample.1.weight'] = dtt_bp + norm_suffix + '_s'
            mapping_to_detectron[my_prefix
                                 + '.downsample.1.bias'] = dtt_bp + norm_suffix + '_b'

        # conv branch
        for i, c in zip([1, 2, 3], ['a', 'b', 'c']):
            dtt_bp = detectron_prefix + '_branch2' + c
            mapping_to_detectron[my_prefix
                                 + '.conv%d.weight' % i] = dtt_bp + '_w'
            orphan_in_detectron.append(dtt_bp + '_b')
            mapping_to_detectron[my_prefix
                                 + '.' + norm_suffix[1:] + '%d.weight' % i] = dtt_bp + norm_suffix + '_s'
            mapping_to_detectron[my_prefix
                                 + '.' + norm_suffix[1:] + '%d.bias' % i] = dtt_bp + norm_suffix + '_b'

    return mapping_to_detectron, orphan_in_detectron


def freeze_params(m):
    """Freeze all the weights by setting requires_grad to False
    """
    for p in m.parameters():
        p.requires_grad = False
