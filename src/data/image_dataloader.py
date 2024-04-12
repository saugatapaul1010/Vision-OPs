"""This module contains a class and functions to create an image data loader
with(out) transformations.
"""

import random

import albumentations as A
import cv2
import numpy as np
import pandas as pd
import torch
import torchvision.transforms as T
from torch.utils.data import DataLoader, Dataset, Subset
from torchvision.ops import box_convert

from src.utils import collate_batch, stratified_group_train_test_split

# Set partial reproducibility
SEED = 0
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)

BBOX_FORMATS = {'coco': 'xywh',
                'pascal_voc': 'xyxy',
                'yolo': 'cxcywh'}


def get_image_transforms(box_format):
    """Return the transform function that will perform image augmentation.

    Image transformations with always_apply=True are applied with 100% probability
    even if their parent containers are not applied. Refer to
    https://albumentations.ai/docs/getting_started/setting_probabilities/
    for more information on how calculated actual probability of other transformations
    and containers in the augmentation pipeline.
    """
    aug = A.Compose([
                    A.SmallestMaxSize(800, always_apply=True),
                    A.LongestMaxSize(1333, always_apply=True),
                    A.HorizontalFlip(p=0.6),
                    A.VerticalFlip(p=0.4),
                    A.ColorJitter(0.5, 0.5, 0.5, 0, p=0.7),
                    A.RandomRain(p=0.5),
                    A.OneOrOther(
                        A.Blur(10, p=0.7),
                        A.GaussianBlur((11, 21), p=0.3),
                        p=0.6),
                    ],
                    A.BboxParams(format=box_format, label_fields=['labels']),
                    p=0.8)
    return aug


class ImageBBoxDataset(Dataset):
    """A Dataset for object detection tasks."""

    def __init__(self, csv_file_path, img_dir_path, bbox_path,
                 img_transforms=None, bbox_transform=None):
        self.img_dir_path = img_dir_path
        self.img_df = pd.read_csv(csv_file_path)
        self.bbox_df = pd.read_csv(bbox_path)
        self.img_transforms = img_transforms
        self.bbox_transform = bbox_transform  # (bbox_transform_fn, *bbox_transform_args)

    def __len__(self):
        return self.img_df.shape[0]

    def __getitem__(self, idx):
        img_name = self.img_df.iloc[idx, 0]
        img_path = self.img_dir_path / img_name
        image = cv2.cvtColor(cv2.imread(str(img_path)), cv2.COLOR_BGR2RGB)
        bboxes = self.bbox_df.loc[(self.bbox_df.image_name == img_name),
                                  ['bbox_x', 'bbox_y', 'bbox_width', 'bbox_height']].values
        labels = torch.ones((bboxes.shape[0],), dtype=torch.int64)

        if self.img_transforms:
            aug = self.img_transforms(image=image, bboxes=bboxes, labels=labels)
            image = aug['image']
            bboxes = aug['bboxes']

        image = T.ToTensor()(image)
        bboxes = torch.as_tensor(bboxes, dtype=torch.float)

        if self.bbox_transform:
            bboxes = self.bbox_transform[0](bboxes, *self.bbox_transform[1:])

        target = {'boxes': bboxes,
                  'labels': labels}

        return image, target


def create_dataloaders(img_dir_path, csv_file_path, bboxes_path, batch_size,
                       box_format_before_transform='coco', train_test_split_data=False,
                       transform_train_imgs=False):
    """Return one DataLoader object (or two if train_test_split_data=True) with applying
    a box transformation to pascal_voc ('xyxy') format and training image
    transformations if necessary.
    """
    # Set ImageBBoxDataset parameters
    img_transforms = (get_image_transforms(box_format_before_transform)
                      if transform_train_imgs else None)
    bbox_transform = None

    if box_format_before_transform != 'pascal_voc':
        bbox_transform = (box_convert, BBOX_FORMATS[box_format_before_transform],
                          BBOX_FORMATS['pascal_voc'])

    dataset_params = {'img_dir_path': img_dir_path,
                      'bbox_path': bboxes_path,
                      'bbox_transform': bbox_transform}

    dl_params = {'batch_size': batch_size,
                 'collate_fn': collate_batch}

    if train_test_split_data:
        # Create ImageBBoxDataset objects
        train_dataset, val_dataset = [
            ImageBBoxDataset(csv_file_path,
                             img_transforms=img_tr,
                             **dataset_params) for img_tr in [img_transforms, None]]

        # Split data into training and validation sets
        train_ids, val_ids = stratified_group_train_test_split(train_dataset.img_df['Name'],
                                                               train_dataset.img_df['Number_HSparrows'],
                                                               train_dataset.img_df['Author'],
                                                               SEED)
        # Create DataLoader objects
        train_dataloader = DataLoader(Subset(train_dataset, train_ids), shuffle=True,
                                      **dl_params)
        val_dataloader = DataLoader(Subset(val_dataset, val_ids), **dl_params)
        return train_dataloader, val_dataloader
    else:
        test_dataset = ImageBBoxDataset(csv_file_path, **dataset_params)
        test_dataloader = DataLoader(test_dataset, **dl_params)
        return test_dataloader
