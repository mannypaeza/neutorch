import math
import random
from functools import cached_property

import numpy as np
import torch
from chunkflow.lib.cartesian_coordinate import Cartesian
from yacs.config import CfgNode

from neutorch.data.sample import SemanticSample
from neutorch.data.transform import *

DEFAULT_PATCH_SIZE = Cartesian(128, 128, 128)

def load_cfg(cfg_file: str, freeze: bool = True):
    with open(cfg_file) as file:
        cfg = CfgNode.load_cfg(file)
    if freeze:
        cfg.freeze()
    return cfg

def worker_init_fn(worker_id: int):
    worker_info = torch.utils.data.get_worker_info()
    
    # the dataset copy in this worker process
    dataset = worker_info.dataset
    overall_start = 0
    overall_end = dataset.sample_num

    # configure the dataset to only process the split workload
    per_worker = int(math.ceil(
        (overall_end - overall_start) / float(worker_info.num_workers)))
    worker_id = worker_info.id
    dataset.start = overall_start + worker_id * per_worker
    dataset.end = min(dataset.start + per_worker, overall_end)

def path_to_dataset_name(path: str, dataset_names: list):
    for dataset_name in dataset_names:
        if dataset_name in path:
            return dataset_name

def to_tensor(arr):
    if isinstance(arr, np.ndarray):
        # Pytorch only supports types: float64, float32, float16, complex64, complex128, int64, int32, int16, int8, uint8, and bool.
        if np.issubdtype(arr.dtype, np.uint16):
            arr = arr.astype(np.int32)
        elif np.issubdtype(arr.dtype, np.uint64):
            arr = arr.astype(np.int64)
        arr = torch.tensor(arr)
    if torch.cuda.is_available():
        arr = arr.cuda()
    return arr


class DatasetBase(torch.utils.data.IterableDataset):
    def __init__(self,
            samples: list, 
        ):
        """
        Parameters:
            patch_size (int or tuple): the patch size we are going to provide.
        """
        super().__init__()
        self.samples = samples

    @cached_property
    def sample_num(self):
        return len(self.samples)
    
    @cached_property
    def sample_weights(self):
        """use the number of candidate patches as volume sampling weight
        if there is None, replace it with average value
        """
        sample_weights = []
        for sample in self.samples:
            sample_weights.append(sample.sampling_weight)

        # replace None weight with average weight 
        ws = []
        for x in sample_weights:
            if x is not None:
                ws.append(x)
        average_weight = np.mean(ws)
        for idx, w in enumerate(sample_weights):
            if w is None:
                sample_weights[idx] = average_weight 
        return sample_weights

    @property
    def random_patch(self):
         # only sample one subject, so replacement option could be ignored
        sample_index = random.choices(
            range(0, self.sample_num),
            weights=self.sample_weights,
            k=1,
        )[0]
        sample = self.samples[sample_index]
        patch = sample.random_patch
        # patch.to_tensor()
        return patch.image, patch.label
   
    def __next__(self):
        image, label = self.random_patch
        image = to_tensor(image)
        label = to_tensor(label)
        return image, label

    def __iter__(self):
        """generate random patches from samples

        Yields:
            tuple[tensor, tensor]: image and label tensors
        """
        while True:
            yield next(self)


class SemanticDataset(DatasetBase):
    def __init__(self, samples: list):
            #patch_size: Cartesian = DEFAULT_PATCH_SIZE):
        super().__init__(samples)
    
    @classmethod
    def from_config(cls, cfg: CfgNode, is_train: bool, **kwargs):
        """Construct a semantic dataset with chunk or volume

        Args:
            cfg (CfgNode): _description_
            is_train (bool): _description_

        Returns:
            _type_: _description_
        """
        if is_train:
            name2chunks = cfg.dataset.training
        else:
            name2chunks = cfg.dataset.validation

        samples = []
        for name2path in name2chunks.values():
            sample = SemanticSample.from_explicit_dict(
                    name2path, 
                    output_patch_size=cfg.train.patch_size,
                    num_classes=cfg.model.out_channels,
                    **kwargs)
            samples.append(sample)

        return cls( samples )

    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustBrightness(),
            AdjustContrast(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            MaskBox(),
            Perspective2D(),
            # RotateScale(probability=1.),
            # DropSection(),
            Flip(),
            Transpose(),
            MissAlignment(),
        ])

class OrganelleDataset(SemanticDataset):
    def __init__(self, samples: list, 
            num_classes: int = 1,
            skip_classes: list = None,
            selected_classes: list = None):
        """Dataset for organelle semantic segmentation

        Args:
            paths (list): list of samples
            sample_name_to_image_versions (dict, optional): map sample name to image volumes. Defaults to None.
            patch_size (Cartesian, optional): size of a output patch. Defaults to Cartesian(128, 128, 128).
            num_classes (int, optional): number of semantic classes to be classified. Defaults to 1.
            skip_classes (list, optional): skip some classes in the label. Defaults to None.
        """
        super().__init__(samples)

        self.samples = samples
        self.num_classes = num_classes
        
        if skip_classes is not None:
            skip_classes = sorted(skip_classes, reverse=True)
        self.skip_classes = skip_classes

        self.selected_classes = selected_classes

    @classmethod
    def from_path_list(cls, path_list: list,
            patch_size: Cartesian = Cartesian(128, 128, 128),
            num_classes: int = 1,
            skip_classes: list = None,
            selected_classes: list = None):
        path_list = sorted(path_list)
        samples = []
        # for img_path, sem_path in zip(self.path_list[0::2], self.path_list[1::2]):
        for label_path in path_list:
            sample = OrganelleSample.from_label_path(
                label_path, 
                num_classes, 
                patch_size=self.patch_size_before_transform,
                skip_classes=skip_classes,
                selected_classes = selected_classes,
            ) 
            
            samples.append(sample)
        
        return cls(samples, patch_size, num_classes, skip_classes, selected_classes)

    @cached_property
    def voxel_num(self):
        voxel_nums = [sample.voxel_num for sample in self.samples]
        return sum(voxel_nums)

    @cached_property
    def class_counts(self):
        counts = np.zeros((self.num_classes,), dtype=np.int)
        for sample in self.samples:
            counts += sample.class_counts

        return counts
     
    def __next__(self):
        # get numpy arrays of image and label
        image, label = self.random_patch
        # if label.ndim == 5:
        #     # the CrossEntropyLoss do not require channel axis
        #     label = np.squeeze(label, axis=1)
        
        # transform to PyTorch Tensor
        # transfer to device, e.g. GPU, automatically.
        image = to_tensor(image)
        target = to_tensor(label)

        return image, target


class AffinityMapDataset(SemanticDataset):
    def __init__(self, samples: list):
        super().__init__(samples)
    
    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustBrightness(),
            AdjustContrast(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            MaskBox(),
            Perspective2D(),
            # RotateScale(probability=1.),
            # DropSection(),
            Flip(),
            Transpose(),
            MissAlignment(),
            Label2AffinityMap(probability=1.),
        ])

class BoundaryAugmentationDataset(SemanticDataset): 
    def __initi__(self, samples: list):
        super.__init__(samples)

    @cached_property
    def transform(self):
        return Compose([
            NormalizeTo01(probability=1.),
            AdjustBrightness(),
            AdjustContrast(),
            Gamma(),
            OneOf([
                Noise(),
                GaussianBlur2D(),
            ]),
            MaskBox(),
            Perspective2D(),
            # RotateScale(probability=1.),
            # DropSection(),
            Flip(),
            Transpose(),
            MissAlignment(),
        ])

if __name__ == '__main__':

    from yacs.config import CfgNode

    cfg_file = '/mnt/home/jwu/wasp/jwu/15_rna_granule_net/11/config.yaml'
    with open(cfg_file) as file:
        cfg = CfgNode.load_cfg(file)
    cfg.freeze()

    sd = BoundaryAugmentationDataset(
        path_list=['/mnt/ceph/users/neuro/wasp_em/jwu/40_gt/13_wasp_sample3/vol_01700/rna_v1.h5'],
        sample_name_to_image_versions=cfg.dataset.sample_name_to_image_versions,
        patch_size=Cartesian(128, 128, 128),
    )
    