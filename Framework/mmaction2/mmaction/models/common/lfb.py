# Copyright (c) OpenMMLab. All rights reserved.
import io
import os.path as osp
import warnings

import numpy as np
import torch
import torch.distributed as dist
from mmcv.runner import get_dist_info

try:
    import lmdb
    lmdb_imported = True
except (ImportError, ModuleNotFoundError):
    lmdb_imported = False


class LFB:
    """Long-Term Feature Bank (LFB).

    LFB is proposed in `Long-Term Feature Banks for Detailed Video
    Understanding <https://arxiv.org/abs/1812.05038>`_

    The ROI features of videos are stored in the feature bank. The feature bank
    was generated by inferring with a lfb infer config.

    Formally, LFB is a Dict whose keys are video IDs and its values are also
    Dicts whose keys are timestamps in seconds. Example of LFB:

    .. code-block:: Python
        {
            '0f39OWEqJ24': {
                901: tensor([[ 1.2760,  1.1965,  ...,  0.0061, -0.0639],
                    [-0.6320,  0.3794,  ..., -1.2768,  0.5684],
                    [ 0.2535,  1.0049,  ...,  0.4906,  1.2555],
                    [-0.5838,  0.8549,  ..., -2.1736,  0.4162]]),
                ...
                1705: tensor([[-1.0169, -1.1293,  ...,  0.6793, -2.0540],
                    [ 1.2436, -0.4555,  ...,  0.2281, -0.8219],
                    [ 0.2815, -0.0547,  ..., -0.4199,  0.5157]]),
                ...
            },
            'xmqSaQPzL1E': {
                ...
            },
            ...
        }

    Args:
        lfb_prefix_path (str): The storage path of lfb.
        max_num_sampled_feat (int): The max number of sampled features.
            Default: 5.
        window_size (int): Window size of sampling long term feature.
            Default: 60.
        lfb_channels (int): Number of the channels of the features stored
            in LFB. Default: 2048.
        dataset_modes (tuple[str] | str): Load LFB of datasets with different
            modes, such as training, validation, testing datasets. If you don't
            do cross validation during training, just load the training dataset
            i.e. setting `dataset_modes = ('train')`.
            Default: ('train', 'val').
        device (str): Where to load lfb. Choices are 'gpu', 'cpu' and 'lmdb'.
            A 1.65GB half-precision ava lfb (including training and validation)
            occupies about 2GB GPU memory. Default: 'gpu'.
        lmdb_map_size (int): Map size of lmdb. Default: 4e9.
        construct_lmdb (bool): Whether to construct lmdb. If you have
            constructed lmdb of lfb, you can set to False to skip the
            construction. Default: True.
    """

    def __init__(self,
                 lfb_prefix_path,
                 max_num_sampled_feat=5,
                 window_size=60,
                 lfb_channels=2048,
                 dataset_modes=('train', 'val'),
                 device='gpu',
                 lmdb_map_size=4e9,
                 construct_lmdb=True):
        if not osp.exists(lfb_prefix_path):
            raise ValueError(
                f'lfb prefix path {lfb_prefix_path} does not exist!')
        self.lfb_prefix_path = lfb_prefix_path
        self.max_num_sampled_feat = max_num_sampled_feat
        self.window_size = window_size
        self.lfb_channels = lfb_channels
        if not isinstance(dataset_modes, tuple):
            assert isinstance(dataset_modes, str)
            dataset_modes = (dataset_modes, )
        self.dataset_modes = dataset_modes
        self.device = device

        rank, world_size = get_dist_info()

        # Loading LFB
        if self.device == 'gpu':
            self.load_lfb(f'cuda:{rank}')
        elif self.device == 'cpu':
            if world_size > 1:
                warnings.warn(
                    'If distributed training is used with multi-GPUs, lfb '
                    'will be loaded multiple times on RAM. In this case, '
                    "'lmdb' is recommended.", UserWarning)
            self.load_lfb('cpu')
        elif self.device == 'lmdb':
            assert lmdb_imported, (
                'Please install `lmdb` to load lfb on lmdb!')
            self.lmdb_map_size = lmdb_map_size
            self.construct_lmdb = construct_lmdb
            self.lfb_lmdb_path = osp.normpath(
                osp.join(self.lfb_prefix_path, 'lmdb'))

            if rank == 0 and self.construct_lmdb:
                print('Constructing LFB lmdb...')
                self.load_lfb_on_lmdb()

            # Synchronizes all processes to make sure lfb lmdb exist.
            if world_size > 1:
                dist.barrier()
            self.lmdb_env = lmdb.open(self.lfb_lmdb_path, readonly=True)
        else:
            raise ValueError("Device must be 'gpu', 'cpu' or 'lmdb', ",
                             f'but get {self.device}.')

    def load_lfb(self, map_location):
        self.lfb = {}
        for dataset_mode in self.dataset_modes:
            lfb_path = osp.normpath(
                osp.join(self.lfb_prefix_path, f'lfb_{dataset_mode}.pkl'))
            print(f'Loading LFB from {lfb_path}...')
            self.lfb.update(torch.load(lfb_path, map_location=map_location))
        print(f'LFB has been loaded on {map_location}.')

    def load_lfb_on_lmdb(self):
        lfb = {}
        for dataset_mode in self.dataset_modes:
            lfb_path = osp.normpath(
                osp.join(self.lfb_prefix_path, f'lfb_{dataset_mode}.pkl'))
            lfb.update(torch.load(lfb_path, map_location='cpu'))

        lmdb_env = lmdb.open(self.lfb_lmdb_path, map_size=self.lmdb_map_size)
        for key, value in lfb.items():
            txn = lmdb_env.begin(write=True)
            buff = io.BytesIO()
            torch.save(value, buff)
            buff.seek(0)
            txn.put(key.encode(), buff.read())
            txn.commit()
            buff.close()

        print(f'LFB lmdb has been constructed on {self.lfb_lmdb_path}!')

    def sample_long_term_features(self, video_id, timestamp):
        if self.device == 'lmdb':
            with self.lmdb_env.begin(write=False) as txn:
                buf = txn.get(video_id.encode())
                video_features = torch.load(io.BytesIO(buf))
        else:
            video_features = self.lfb[video_id]

        # Sample long term features.
        window_size, K = self.window_size, self.max_num_sampled_feat
        start = timestamp - (window_size // 2)
        lt_feats = torch.zeros(window_size * K, self.lfb_channels)

        for idx, sec in enumerate(range(start, start + window_size)):
            if sec in video_features:
                # `num_feat` is the number of roi features in this second.
                num_feat = len(video_features[sec])
                num_feat_sampled = min(num_feat, K)
                # Sample some roi features randomly.
                random_lfb_indices = np.random.choice(
                    range(num_feat), num_feat_sampled, replace=False)

                for k, rand_idx in enumerate(random_lfb_indices):
                    lt_feats[idx * K + k] = video_features[sec][rand_idx]

        # [window_size * max_num_sampled_feat, lfb_channels]
        return lt_feats

    def __getitem__(self, img_key):
        """Sample long term features like `lfb['0f39OWEqJ24,0902']` where `lfb`
        is a instance of class LFB."""
        video_id, timestamp = img_key.split(',')
        return self.sample_long_term_features(video_id, int(timestamp))

    def __len__(self):
        """The number of videos whose ROI features are stored in LFB."""
        return len(self.lfb)