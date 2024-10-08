# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
COCO dataset which returns image_id for evaluation.

Mostly copy-paste from https://github.com/pytorch/vision/blob/13b35ff/references/detection/coco_utils.py
"""
from pathlib import Path

import torch
import torch.utils.data
from pycocotools import mask as coco_mask

from .torchvision_datasets import CocoDetection as TvCocoDetection
from util.misc import get_local_rank, get_local_size
import datasets.transforms as T
from datasets.augmentation import *

class CocoDetection(TvCocoDetection):
    def __init__(self, img_folder, ann_file, transforms, return_masks,
                 cache_mode=False, local_rank=0, local_size=1, img_ids = None, class_ids = None):
        super(CocoDetection, self).__init__(img_folder, ann_file,
                                            cache_mode=cache_mode, local_rank=local_rank, local_size=local_size, ids_list=img_ids, class_ids=class_ids)
        
        # self.coco.cats가 ann file이 아니라 class_ids를 참조하도록 변경
        cats = {}
        for class_id in class_ids:
            try:
                cats[class_id] = self.coco.cats[class_id]
            except KeyError:
                pass
        self.coco.cats = cats
        
        self._transforms = transforms
        self.prepare = ConvertCocoPolysToMask(return_masks)

    def __getitem__(self, idx):
        img, target = super(CocoDetection, self).__getitem__(idx)
        image_id = self.ids[idx]
        target = {'image_id': image_id, 'annotations': target}
        img, target = self.prepare(img, target)
        if self._transforms is not None:
            img, target = self._transforms(img, target)
        return img, target


def convert_coco_poly_to_mask(segmentations, height, width):
    masks = []
    for polygons in segmentations:
        rles = coco_mask.frPyObjects(polygons, height, width)
        mask = coco_mask.decode(rles)
        if len(mask.shape) < 3:
            mask = mask[..., None]
        mask = torch.as_tensor(mask, dtype=torch.uint8)
        mask = mask.any(dim=2)
        masks.append(mask)
    if masks:
        masks = torch.stack(masks, dim=0)
    else:
        masks = torch.zeros((0, height, width), dtype=torch.uint8)
    return masks


class ConvertCocoPolysToMask(object):
    def __init__(self, return_masks=False):
        self.return_masks = return_masks

    def __call__(self, image, target):
        w, h = image.size

        image_id = target["image_id"]
        image_id = torch.tensor([image_id])

        anno = target["annotations"]

        anno = [obj for obj in anno if 'iscrowd' not in obj or obj['iscrowd'] == 0]

        boxes = [obj["bbox"] for obj in anno]
        # guard against no boxes via resizing
        boxes = torch.as_tensor(boxes, dtype=torch.float32).reshape(-1, 4)
        boxes[:, 2:] += boxes[:, :2]
        boxes[:, 0::2].clamp_(min=0, max=w)
        boxes[:, 1::2].clamp_(min=0, max=h)

        classes = [obj["category_id"] for obj in anno]
        classes = torch.tensor(classes, dtype=torch.int64)

        if self.return_masks:
            segmentations = [obj["segmentation"] for obj in anno]
            masks = convert_coco_poly_to_mask(segmentations, h, w)

        keypoints = None
        if anno and "keypoints" in anno[0]:
            keypoints = [obj["keypoints"] for obj in anno]
            keypoints = torch.as_tensor(keypoints, dtype=torch.float32)
            num_keypoints = keypoints.shape[0]
            if num_keypoints:
                keypoints = keypoints.view(num_keypoints, -1, 3)

        keep = (boxes[:, 3] > boxes[:, 1]) & (boxes[:, 2] > boxes[:, 0])
        boxes = boxes[keep]
        classes = classes[keep]
        if self.return_masks:
            masks = masks[keep]
        if keypoints is not None:
            keypoints = keypoints[keep]

        target = {}
        target["boxes"] = boxes
        target["labels"] = classes
        if self.return_masks:
            target["masks"] = masks
        target["image_id"] = image_id
        if keypoints is not None:
            target["keypoints"] = keypoints

        # for conversion to coco api
        area = torch.tensor([obj["area"] for obj in anno])
        iscrowd = torch.tensor([obj["iscrowd"] if "iscrowd" in obj else 0 for obj in anno])
        target["area"] = area[keep]
        target["iscrowd"] = iscrowd[keep]

        target["orig_size"] = torch.as_tensor([int(h), int(w)])
        target["size"] = torch.as_tensor([int(h), int(w)])

        return image, target
    
def make_coco_transforms(image_set, fix_size=False):

    normalize = T.Compose([
        T.ToTensor(),
        # T.Normalize([0.312, 0.315, 0.294], [0.120, 0.122, 0.131]) # For LG
        T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])# third (11.14 ~ )
    ])

    scales = [480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800]

    if image_set == 'train':
        
        return T.Compose([
            T.RandomHorizontalFlip(),
            T.RandomSelect(   
                T.RandomResize(scales, max_size=1333),
                T.Compose([
                    T.RandomResize([400, 500, 600]),
                    T.RandomSizeCrop(384, 600),
                    T.RandomResize(scales, max_size=1333),
                ])
            ),
            normalize,
        ])
        
    if image_set == 'extra':
        if fix_size:
            return T.Compose([
                T.RandomHorizontalFlip(),
                T.RandomResize(sizes=[(600, 600)], max_size=None),
                normalize,
            ])
        return T.Compose([
            # T.RandomResize([608], max_size=1200),
            normalize,
        ])
        
        
    if image_set == 'val': #* can use to pseudo generation. all adatptions are fixed to only sample dataset config.
        return T.Compose([
            T.RandomResize([800], max_size=1333), 
            normalize, 
        ])

    raise ValueError(f'unknown {image_set}')

def get_paths(args, pseudo=False):
    root = Path(args.coco_path)
    gen_root = Path(args.generator_path) 

    # if pseudo : #* pseudo generation. all adatptions are fixed to only sample dataset config
    #     return {
    #         "train": (gen_root / "images", gen_root / 'annotations' / 'pseudo_data.json'),
    #     }
    # if args.orgcocopath: #* original generation (annotations/annotations2017train.json)

    return {
        "train": ("/mnt/thanhpd/MTSD/mtsd_fully_annotated_train_images/images", 
                    "/mnt/thanhpd/code/mtsd_cl/CL_rtdetr/train_output_file_coco.json"),
        "val": ("/mnt/thanhpd/MTSD/mtsd_v2_fully_annotated_images.val.zip/images", 
                "/mnt/thanhpd/code/mtsd_cl/CL_rtdetr/val_output_file_coco.json"),
        # "extra": (root / "train2017", root / "annotations" / f'{mode}_train2017.json'),
    }

    # else:
    #     return { #* original gneration, but other variational loading version
    #         "train": (root / "train/images", root / 'train/output_json' / 'train.json'),
    #         "val": (root / "test/images", root / 'test/output_json' / 'test.json'),
    #         # "extra": (root / "train/images", root / 'train/output_json' / 'train.json'),
    #     }


def build(image_set, args, img_ids=None, class_ids=None, pseudo=False):    
    PATHS = get_paths(args, pseudo)
    
    print(args.coco_path)
    print(PATHS)

    img_folder, ann_file = PATHS[image_set]
    dataset = CocoDetection(img_folder, ann_file, transforms=make_coco_transforms(image_set, args.Sampling_strategy == "icarl"), return_masks=args.masks,
                            cache_mode=args.cache_mode, local_rank=get_local_rank(), local_size=get_local_size(), img_ids=img_ids, class_ids=class_ids)
    return dataset
