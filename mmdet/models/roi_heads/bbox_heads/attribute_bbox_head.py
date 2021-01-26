import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.runner import auto_fp16, force_fp32
from torch.nn.modules.utils import _pair

from mmdet.core import build_bbox_coder, multi_apply
from mmdet.models.builder import HEADS, build_loss
from mmdet.models.losses import accuracy


"""
Here we bundle the extended boudning box head together with the modified helper
functions required to handle the 5 output layers. Helper functions could be 
relocated in refactoring.
"""


@HEADS.register_module()
class AttributeBBoxHead(nn.Module):
    """RoI head, with 5 fc layers for classification,
    regression, face, colour, motion respectively."""

    def __init__(self,
                 with_avg_pool=False,
                 with_cls=True,
                 with_reg=True,
                 with_face=True, #
                 with_colour=True, #
                 with_motion=True, #
                 roi_feat_size=7,
                 in_channels=256,
                 num_classes=31, #
                 num_faces=3, #
                 num_colours=7, #
                 num_motions=2, #
                 bbox_coder=dict(
                     type='DeltaXYWHBBoxCoder',
                     clip_border=True,
                     target_means=[0., 0., 0., 0.],
                     target_stds=[0.1, 0.1, 0.2, 0.2]),
                 reg_class_agnostic=False,
                 reg_decoded_bbox=False,
                 loss_cls=dict(
                     type='CrossEntropyLoss',
                     use_sigmoid=False,
                     loss_weight=1.0),
                 loss_face=dict(
                     type='CrossEntropyLoss',
                     use_sigmoid=False,
                     loss_weight=1.0),#
                 loss_colour=dict(
                     type='CrossEntropyLoss',
                     use_sigmoid=False,
                     loss_weight=1.0),#
                 loss_motion=dict(
                     type='CrossEntropyLoss',
                     use_sigmoid=False,
                     loss_weight=1.0),#
                 loss_bbox=dict(
                     type='SmoothL1Loss', beta=1.0, loss_weight=1.0)):
        super(BBoxHead, self).__init__()
        assert with_cls or with_reg or with_face or with_colour or with_motion #
        self.with_avg_pool = with_avg_pool
        self.with_cls = with_cls
        self.with_reg = with_reg
        self.with_face = with_face#
        self.with_colour = with_colour#
        self.with_motion = with_motion#
        self.roi_feat_size = _pair(roi_feat_size)
        self.roi_feat_area = self.roi_feat_size[0] * self.roi_feat_size[1]
        self.in_channels = in_channels
        self.num_classes = num_classes
        self.num_faces = num_faces#
        self.num_colours = num_colours#
        self.num_motions = num_motions#
        self.reg_class_agnostic = reg_class_agnostic
        self.reg_decoded_bbox = reg_decoded_bbox
        self.fp16_enabled = False

        self.bbox_coder = build_bbox_coder(bbox_coder)
        self.loss_cls = build_loss(loss_cls)
        self.loss_bbox = build_loss(loss_bbox)
        self.loss_face = build_loss(loss_face)#
        self.loss_colour = build_loss(loss_colour)#
        self.loss_motion = build_loss(loss_motion)#

        in_channels = self.in_channels
        if self.with_avg_pool:
            self.avg_pool = nn.AvgPool2d(self.roi_feat_size)
        else:
            in_channels *= self.roi_feat_area
        if self.with_cls:
            # need to add background class
            self.fc_cls = nn.Linear(in_channels, num_classes + 1)
        if self.with_reg:
            out_dim_reg = 4 if reg_class_agnostic else 4 * num_classes
            self.fc_reg = nn.Linear(in_channels, out_dim_reg)
        if self.with_face:#
            self.fc_face = nn.Linear(in_channels, num_faces)#
        if self.with_colour:#
            self.fc_colour = nn.Linear(in_channels, num_colours)#
        if self.with_motion:#
            self.fc_motion = nn.Linear(in_channels, num_motions)#
        self.debug_imgs = None

    def init_weights(self):
        # conv layers are already initialized by ConvModule
        if self.with_cls:
            nn.init.normal_(self.fc_cls.weight, 0, 0.01)
            nn.init.constant_(self.fc_cls.bias, 0)
        if self.with_reg:
            nn.init.normal_(self.fc_reg.weight, 0, 0.001)
            nn.init.constant_(self.fc_reg.bias, 0)
        if self.with_face:#
            nn.init.normal_(self.fc_face.weight, 0, 0.01)#
            nn.init.constant_(self.fc_face.bias, 0)#
        if self.with_colour:#
            nn.init.normal_(self.fc_colour.weight, 0, 0.01)#
            nn.init.constant_(self.fc_colour.bias, 0)#
        if self.with_motion:#
            nn.init.normal_(self.fc_motion.weight, 0, 0.01)#
            nn.init.constant_(self.fc_motion.bias, 0)#

    @auto_fp16()
    def forward(self, x):
        if self.with_avg_pool:
            x = self.avg_pool(x)
        x = x.view(x.size(0), -1)
        cls_score = self.fc_cls(x) if self.with_cls else None
        bbox_pred = self.fc_reg(x) if self.with_reg else None
        face_score = self.fc_face(x) if self.with_face else None#
        colour_score = self.fc_colour(x) if self.with_colour else None#
        motion_score = self.fc_motion(x) if self.with_motion else None#
        return cls_score, bbox_pred, face_score, colour_score, motion_score#

    def _get_target_single(self, pos_bboxes, neg_bboxes, pos_gt_bboxes,
                           pos_gt_labels, pos_gt_faces, pos_gt_colours,
                           pos_gt_motions, cfg):
        num_pos = pos_bboxes.size(0)
        num_neg = neg_bboxes.size(0)
        num_samples = num_pos + num_neg

        # original implementation uses new_zeros since BG are set to be 0
        # now use empty & fill because BG cat_id = num_classes,
        # FG cat_id = [0, num_classes-1]
        labels = pos_bboxes.new_full((num_samples, ),
                                     self.num_classes,
                                     dtype=torch.long)
        label_weights = pos_bboxes.new_zeros(num_samples)
        faces = pos_bboxes.new_full((num_samples, ),
                                     self.num_faces,
                                     dtype=torch.long)#
        face_weights = pos_bboxes.new_zeros(num_samples)#
        colours = pos_bboxes.new_full((num_samples, ),
                                     self.num_colours,
                                     dtype=torch.long)#
        colour_weights = pos_bboxes.new_zeros(num_samples)#
        motions = pos_bboxes.new_full((num_samples, ),
                                     self.num_motions,
                                     dtype=torch.long)#
        motion_weights = pos_bboxes.new_zeros(num_samples)#
        bbox_targets = pos_bboxes.new_zeros(num_samples, 4)
        bbox_weights = pos_bboxes.new_zeros(num_samples, 4)
        if num_pos > 0:
            labels[:num_pos] = pos_gt_labels
            faces[:num_pos] = pos_gt_faces#
            colours[:num_pos] = pos_gt_colours#
            motions[:num_pos] = pos_gt_motions#
            pos_weight = 1.0 if cfg.pos_weight <= 0 else cfg.pos_weight
            label_weights[:num_pos] = pos_weight
            face_weights[:num_pos] = pos_weight#
            colour_weights[:num_pos] = pos_weight#
            motion_weights[:num_pos] = pos_weight#
            if not self.reg_decoded_bbox:
                pos_bbox_targets = self.bbox_coder.encode(
                    pos_bboxes, pos_gt_bboxes)
            else:
                # When the regression loss (e.g. `IouLoss`, `GIouLoss`)
                # is applied directly on the decoded bounding boxes, both
                # the predicted boxes and regression targets should be with
                # absolute coordinate format.
                pos_bbox_targets = pos_gt_bboxes
            bbox_targets[:num_pos, :] = pos_bbox_targets
            bbox_weights[:num_pos, :] = 1
        if num_neg > 0:
            label_weights[-num_neg:] = 1.0
            face_weights[-num_neg:] = 1.0#
            colour_weights[-num_neg:] = 1.0#
            motion_weights[-num_neg:] = 1.0#

        return labels, label_weights, bbox_targets, bbox_weights, faces, face_weights, colours, colour_weights, motions, motion_weights#

    def get_targets(self,
                    sampling_results,
                    gt_bboxes,
                    gt_labels,
                    gt_faces,#
                    gt_colours,#
                    gt_motions,#
                    rcnn_train_cfg,
                    concat=True):
        pos_bboxes_list = [res.pos_bboxes for res in sampling_results]
        neg_bboxes_list = [res.neg_bboxes for res in sampling_results]
        pos_gt_bboxes_list = [res.pos_gt_bboxes for res in sampling_results]
        pos_gt_labels_list = [res.pos_gt_labels for res in sampling_results]
        pos_gt_faces_list = [res.pos_gt_faces for res in sampling_results]#
        pos_gt_colours_list = [res.pos_gt_colours for res in sampling_results]#
        pos_gt_motions_list = [res.pos_gt_motions for res in sampling_results]#
        labels, label_weights, bbox_targets, bbox_weights, faces, face_weights, colours, colour_weights, motions, motion_weights = multi_apply(
            self._get_target_single,
            pos_bboxes_list,
            neg_bboxes_list,
            pos_gt_bboxes_list,
            pos_gt_labels_list,
            pos_gt_faces_list,#
            pos_gt_colours_list,#
            pos_gt_motions_list,#
            cfg=rcnn_train_cfg)

        if concat:
            labels = torch.cat(labels, 0)
            label_weights = torch.cat(label_weights, 0)
            faces = torch.cat(faces, 0)#
            face_weights = torch.cat(face_weights, 0)#
            colours = torch.cat(colours, 0)#
            colour_weights = torch.cat(colour_weights, 0)#
            motions = torch.cat(motions, 0)#
            motion_weights = torch.cat(motion_weights, 0)#
            bbox_targets = torch.cat(bbox_targets, 0)
            bbox_weights = torch.cat(bbox_weights, 0)
            
        return labels, label_weights, bbox_targets, bbox_weights, faces, face_weights, colours, colour_weights, motions, motion_weights#

    @force_fp32(apply_to=('cls_score', 'bbox_pred', 'face_score', 'colour_score', 'motion_score'))
    def loss(self,
             cls_score,
             bbox_pred,
             face_score,#
             colour_score,#
             motion_score,#
             rois,
             labels,
             label_weights,
             bbox_targets,
             bbox_weights,
             faces,#
             face_weights,#
             colours,#
             colour_weights,#
             motions,#
             motion_weights,#
             reduction_override=None):
        losses = dict()
        if cls_score is not None:
            avg_factor = max(torch.sum(label_weights > 0).float().item(), 1.)
            if cls_score.numel() > 0:
                losses['loss_cls'] = self.loss_cls(
                    cls_score,
                    labels,
                    label_weights,
                    avg_factor=avg_factor,
                    reduction_override=reduction_override)
                losses['acc'] = accuracy(cls_score, labels)
        if face_score is not None:
            avg_factor = max(torch.sum(face_weights > 0).float().item(), 1.)
            if face_score.numel() > 0:
                losses['loss_face'] = self.loss_face(
                    face_score,
                    faces,
                    face_weights,
                    avg_factor=avg_factor,
                    reduction_override=reduction_override)
                #losses['acc'] = accuracy(face_score, faces)
        if colour_score is not None:
            avg_factor = max(torch.sum(colour_weights > 0).float().item(), 1.)
            if colour_score.numel() > 0:
                losses['colour_face'] = self.loss_colour(
                    colour_score,
                    colours,
                    colour_weights,
                    avg_factor=avg_factor,
                    reduction_override=reduction_override)
                #losses['acc'] = accuracy(face_score, faces)
        if motion_score is not None:
            avg_factor = max(torch.sum(motion_weights > 0).float().item(), 1.)
            if motion_score.numel() > 0:
                losses['loss_motion'] = self.loss_motion(
                    motion_score,
                    motions,
                    motions_weights,
                    avg_factor=avg_factor,
                    reduction_override=reduction_override)
                #losses['acc'] = accuracy(face_score, faces)
        if bbox_pred is not None:
            bg_class_ind = self.num_classes
            # 0~self.num_classes-1 are FG, self.num_classes is BG
            pos_inds = (labels >= 0) & (labels < bg_class_ind)
            # do not perform bounding box regression for BG anymore.
            if pos_inds.any():
                if self.reg_decoded_bbox:
                    # When the regression loss (e.g. `IouLoss`,
                    # `GIouLoss`, `DIouLoss`) is applied directly on
                    # the decoded bounding boxes, it decodes the
                    # already encoded coordinates to absolute format.
                    bbox_pred = self.bbox_coder.decode(rois[:, 1:], bbox_pred)
                if self.reg_class_agnostic:
                    pos_bbox_pred = bbox_pred.view(
                        bbox_pred.size(0), 4)[pos_inds.type(torch.bool)]
                else:
                    pos_bbox_pred = bbox_pred.view(
                        bbox_pred.size(0), -1,
                        4)[pos_inds.type(torch.bool),
                           labels[pos_inds.type(torch.bool)]]
                losses['loss_bbox'] = self.loss_bbox(
                    pos_bbox_pred,
                    bbox_targets[pos_inds.type(torch.bool)],
                    bbox_weights[pos_inds.type(torch.bool)],
                    avg_factor=bbox_targets.size(0),
                    reduction_override=reduction_override)
            else:
                losses['loss_bbox'] = bbox_pred[pos_inds].sum()
        return losses

    @force_fp32(apply_to=('cls_score', 'bbox_pred', 'face_score', 'colour_score', 'motion_score'))
    def get_bboxes(self,
                   rois,
                   cls_score,
                   bbox_pred,
                   face_score,
                   colour_score,
                   motion_score,
                   img_shape,
                   scale_factor,
                   rescale=False,
                   cfg=None):
        if isinstance(cls_score, list):
            cls_score = sum(cls_score) / float(len(cls_score))
        scores = F.softmax(cls_score, dim=1) if cls_score is not None else None

        if isinstance(face_score, list):
            face_score = sum(face_score) / float(len(face_score))
        face_scores = F.softmax(face_score, dim=1) if face_score is not None else None
        
        if isinstance(colour_score, list):
            colour_scores = sum(colour_score) / float(len(colour_score))
        face_scores = F.softmax(cls_score, dim=1) if colour_score is not None else None
        
        if isinstance(motion_score, list):
            motion_score = sum(motion_score) / float(len(motion_score))
        motion_scores = F.softmax(motion_score, dim=1) if motion_score is not None else None
        
        if bbox_pred is not None:
            bboxes = self.bbox_coder.decode(
                rois[:, 1:], bbox_pred, max_shape=img_shape)
        else:
            bboxes = rois[:, 1:].clone()
            if img_shape is not None:
                bboxes[:, [0, 2]].clamp_(min=0, max=img_shape[1])
                bboxes[:, [1, 3]].clamp_(min=0, max=img_shape[0])

        if rescale and bboxes.size(0) > 0:
            if isinstance(scale_factor, float):
                bboxes /= scale_factor
            else:
                scale_factor = bboxes.new_tensor(scale_factor)
                bboxes = (bboxes.view(bboxes.size(0), -1, 4) /
                          scale_factor).view(bboxes.size()[0], -1)

        if cfg is None:
            return bboxes, scores, face_scores, colour_scores, motion_scores
        else:
            det_bboxes, det_labels, det_faces, det_colours, det_motions = multiclass_nms(bboxes, scores,
                                                    face_scores, colour_scores, motion_scores,
                                                    cfg.score_thr, cfg.nms,
                                                    cfg.max_per_img)

            return det_bboxes, det_labels, det_faces, det_colours, det_motions

# TODO
    @force_fp32(apply_to=('bbox_preds', ))
    def refine_bboxes(self, rois, labels, bbox_preds, pos_is_gts, img_metas):
        """Refine bboxes during training.

        Args:
            rois (Tensor): Shape (n*bs, 5), where n is image number per GPU,
                and bs is the sampled RoIs per image. The first column is
                the image id and the next 4 columns are x1, y1, x2, y2.
            labels (Tensor): Shape (n*bs, ).
            bbox_preds (Tensor): Shape (n*bs, 4) or (n*bs, 4*#class).
            pos_is_gts (list[Tensor]): Flags indicating if each positive bbox
                is a gt bbox.
            img_metas (list[dict]): Meta info of each image.

        Returns:
            list[Tensor]: Refined bboxes of each image in a mini-batch.

        Example:
            >>> # xdoctest: +REQUIRES(module:kwarray)
            >>> import kwarray
            >>> import numpy as np
            >>> from mmdet.core.bbox.demodata import random_boxes
            >>> self = BBoxHead(reg_class_agnostic=True)
            >>> n_roi = 2
            >>> n_img = 4
            >>> scale = 512
            >>> rng = np.random.RandomState(0)
            >>> img_metas = [{'img_shape': (scale, scale)}
            ...              for _ in range(n_img)]
            >>> # Create rois in the expected format
            >>> roi_boxes = random_boxes(n_roi, scale=scale, rng=rng)
            >>> img_ids = torch.randint(0, n_img, (n_roi,))
            >>> img_ids = img_ids.float()
            >>> rois = torch.cat([img_ids[:, None], roi_boxes], dim=1)
            >>> # Create other args
            >>> labels = torch.randint(0, 2, (n_roi,)).long()
            >>> bbox_preds = random_boxes(n_roi, scale=scale, rng=rng)
            >>> # For each image, pretend random positive boxes are gts
            >>> is_label_pos = (labels.numpy() > 0).astype(np.int)
            >>> lbl_per_img = kwarray.group_items(is_label_pos,
            ...                                   img_ids.numpy())
            >>> pos_per_img = [sum(lbl_per_img.get(gid, []))
            ...                for gid in range(n_img)]
            >>> pos_is_gts = [
            >>>     torch.randint(0, 2, (npos,)).byte().sort(
            >>>         descending=True)[0]
            >>>     for npos in pos_per_img
            >>> ]
            >>> bboxes_list = self.refine_bboxes(rois, labels, bbox_preds,
            >>>                    pos_is_gts, img_metas)
            >>> print(bboxes_list)
        """
        img_ids = rois[:, 0].long().unique(sorted=True)
        assert img_ids.numel() <= len(img_metas)

        bboxes_list = []
        for i in range(len(img_metas)):
            inds = torch.nonzero(
                rois[:, 0] == i, as_tuple=False).squeeze(dim=1)
            num_rois = inds.numel()

            bboxes_ = rois[inds, 1:]
            label_ = labels[inds]
            bbox_pred_ = bbox_preds[inds]
            img_meta_ = img_metas[i]
            pos_is_gts_ = pos_is_gts[i]

            bboxes = self.regress_by_class(bboxes_, label_, bbox_pred_,
                                           img_meta_)

            # filter gt bboxes
            pos_keep = 1 - pos_is_gts_
            keep_inds = pos_is_gts_.new_ones(num_rois)
            keep_inds[:len(pos_is_gts_)] = pos_keep

            bboxes_list.append(bboxes[keep_inds.type(torch.bool)])

        return bboxes_list

# TODO
    @force_fp32(apply_to=('bbox_pred', ))
    def regress_by_class(self, rois, label, bbox_pred, img_meta):
        """Regress the bbox for the predicted class. Used in Cascade R-CNN.
        Args:
            rois (Tensor): shape (n, 4) or (n, 5)
            label (Tensor): shape (n, )
            bbox_pred (Tensor): shape (n, 4*(#class)) or (n, 4)
            img_meta (dict): Image meta info.
        Returns:
            Tensor: Regressed bboxes, the same shape as input rois.
        """
        assert rois.size(1) == 4 or rois.size(1) == 5, repr(rois.shape)

        if not self.reg_class_agnostic:
            label = label * 4
            inds = torch.stack((label, label + 1, label + 2, label + 3), 1)
            bbox_pred = torch.gather(bbox_pred, 1, inds)
        assert bbox_pred.size(1) == 4

        if rois.size(1) == 4:
            new_rois = self.bbox_coder.decode(
                rois, bbox_pred, max_shape=img_meta['img_shape'])
        else:
            bboxes = self.bbox_coder.decode(
                rois[:, 1:], bbox_pred, max_shape=img_meta['img_shape'])
            new_rois = torch.cat((rois[:, [0]], bboxes), dim=1)

        return new_rois

#########################################################################
# Helpers to move back to core/utils/mmcv


# TODO
def multiclass_nms(multi_bboxes,
                   multi_scores,
                   multi_faces,
                   multi_colours,
                   multi_motions,
                   score_thr,
                   nms_cfg,
                   max_num=-1,
                   score_factors=None,
                   return_inds=False):
    """
    REWRITE FROM mmdet.core.post_processing

    NMS for multi-class bboxes.

    Args:
        multi_bboxes (Tensor): shape (n, #class*4) or (n, 4)
        multi_scores (Tensor): shape (n, #class), where the last column
            contains scores of the background class, but this will be ignored.
        score_thr (float): bbox threshold, bboxes with scores lower than it
            will not be considered.
        nms_thr (float): NMS IoU threshold
        max_num (int, optional): if there are more than max_num bboxes after
            NMS, only top max_num will be kept. Default to -1.
        score_factors (Tensor, optional): The factors multiplied to scores
            before applying NMS. Default to None.
        return_inds (bool, optional): Whether return the indices of kept
            bboxes. Default to False.

    Returns:
        tuple: (bboxes, labels, indices (optional)), tensors of shape (k, 5),
            (k), and (k). Labels are 0-based.
    """
    num_classes = multi_scores.size(1) - 1
    # exclude background category
    if multi_bboxes.shape[1] > 4:
        bboxes = multi_bboxes.view(multi_scores.size(0), -1, 4)
    else:
        bboxes = multi_bboxes[:, None].expand(
            multi_scores.size(0), num_classes, 4)

    scores = multi_scores[:, :-1]

    labels = torch.arange(num_classes, dtype=torch.long)
    labels = labels.view(1, -1).expand_as(scores)

    bboxes = bboxes.reshape(-1, 4)
    scores = scores.reshape(-1)
    labels = labels.reshape(-1)

    # remove low scoring boxes
    valid_mask = scores > score_thr
    # multiply score_factor after threshold to preserve more bboxes, improve
    # mAP by 1% for YOLOv3
    if score_factors is not None:
        # expand the shape to match original shape of score
        score_factors = score_factors.view(-1, 1).expand(
            multi_scores.size(0), num_classes)
        score_factors = score_factors.reshape(-1)
        scores = scores * score_factors
    inds = valid_mask.nonzero(as_tuple=False).squeeze(1)
    bboxes, scores, labels = bboxes[inds], scores[inds], labels[inds]
    if inds.numel() == 0:
        if torch.onnx.is_in_onnx_export():
            raise RuntimeError('[ONNX Error] Can not record NMS '
                               'as it has not been executed this time')
        if return_inds:
            return bboxes, labels, inds
        else:
            return bboxes, labels

    # TODO: add size check before feed into batched_nms
    dets, keep = batched_nms(bboxes, scores, labels, nms_cfg)

    if max_num > 0:
        dets = dets[:max_num]
        keep = keep[:max_num]

    if return_inds:
        return dets, labels[keep], keep
    else:
        return dets, labels[keep]
