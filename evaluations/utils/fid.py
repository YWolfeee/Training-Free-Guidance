"""Calculates the Frechet Inception Distance (FID) to evalulate GANs

The FID metric calculates the distance between two distributions of images.
Typically, we have summary statistics (mean & covariance matrix) of one
of these distributions, while the 2nd distribution is given by a GAN.

When run as a stand-alone program, it compares the distribution of
images that are stored as PNG/JPEG at a specified location with a
distribution given by summary statistics (in pickle format).

The FID is calculated by assuming that X_1 and X_2 are the activations of
the pool_3 layer of the inception net for generated samples and real world
samples respectively.

See --help to see further details.

Code apapted from https://github.com/bioinf-jku/TTUR to use PyTorch instead
of Tensorflow

Copyright 2018 Institute of Bioinformatics, JKU Linz

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import os
import pathlib
import numpy as np
import torch
from functools import partial
import torchvision.transforms as TF
from PIL import Image
from scipy import linalg
from torch.nn.functional import adaptive_avg_pool2d

try:
    from tqdm import tqdm
except ImportError:
    # If tqdm is not available, provide a mock version of it
    def tqdm(x):
        return x

from .inception import InceptionV3
from .torch_sqrtm import sqrtm, torch_matmul_to_array, np_to_gpu_tensor

from utils.env_utils import *

class ImagePILDataset(torch.utils.data.Dataset):
    def __init__(self, images, transforms=None):
        self.files = images
        self.transforms = transforms

    def __len__(self):
        return len(self.files)

    def __getitem__(self, i):
        image = self.files[i]
        img = image.convert('RGB')
        if self.transforms is not None:
            img = self.transforms(img)
        return img


def get_activations(files, model, batch_size=50, dims=2048, device='cpu',
                    num_workers=1):
    """Calculates the activations of the pool_3 layer for all images.

    Params:
    -- files       : List of image files paths
    -- model       : Instance of inception model
    -- batch_size  : Batch size of images for the model to process at once.
                     Make sure that the number of samples is a multiple of
                     the batch size, otherwise some samples are ignored. This
                     behavior is retained to match the original FID score
                     implementation.
    -- dims        : Dimensionality of features returned by Inception
    -- device      : Device to run calculations
    -- num_workers : Number of parallel dataloader workers

    Returns:
    -- A numpy array of dimension (num images, dims) that contains the
       activations of the given tensor when feeding inception with the
       query tensor.
    """
    model.eval()

    if batch_size > len(files):
        print(('Warning: batch size is bigger than the data size. '
               'Setting batch size to data size'))
        batch_size = len(files)

    dataset = ImagePILDataset(files, transforms=TF.ToTensor())

    dataloader = torch.utils.data.DataLoader(dataset,
                                             batch_size=batch_size,
                                             shuffle=False,
                                             drop_last=False,
                                             num_workers=num_workers)

    pred_arr = np.empty((len(files), dims))

    start_idx = 0

    for batch in tqdm(dataloader):
        batch = batch.to(device)

        with torch.no_grad():
            pred = model(batch)[0]

        # If model output is not scalar, apply global spatial average pooling.
        # This happens if you choose a dimensionality not equal 2048.
        if pred.size(2) != 1 or pred.size(3) != 1:
            pred = adaptive_avg_pool2d(pred, output_size=(1, 1))

        pred = pred.squeeze(3).squeeze(2).cpu().numpy()

        pred_arr[start_idx:start_idx + pred.shape[0]] = pred

        start_idx = start_idx + pred.shape[0]

    return pred_arr


def calculate_frechet_distance(mu1, sigma1, mu2, sigma2, device, eps=1e-6):

    array_to_tensor = partial(np_to_gpu_tensor, device)    
    mu1 = np.atleast_1d(mu1)
    mu2 = np.atleast_1d(mu2)

    sigma1 = np.atleast_2d(sigma1)
    sigma2 = np.atleast_2d(sigma2)

    assert mu1.shape == mu2.shape, \
        'Training and test mean vectors have different lengths'
    assert sigma1.shape == sigma2.shape, \
        'Training and test covariances have different dimensions'

    diff = mu1 - mu2

    # Product might be almost singular
    covmean, _ = sqrtm(torch_matmul_to_array(array_to_tensor(sigma1), array_to_tensor(sigma2)), array_to_tensor, disp=False)

    if not np.isfinite(covmean).all():
        msg = ('fid calculation produces singular product; '
            'adding %s to diagonal of cov estimates') % eps
        print(msg)
        offset = np.eye(sigma1.shape[0]) * eps
        covmean = sqrtm(torch_matmul_to_array(array_to_tensor(sigma1 + offset), array_to_tensor(sigma2 + offset)), array_to_tensor)

    # Numerical error might give slight imaginary component
    if np.iscomplexobj(covmean):
        if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
            m = np.max(np.abs(covmean.imag))
            raise ValueError('Imaginary component {}'.format(m))
        covmean = covmean.real

    tr_covmean = np.trace(covmean)

    diff_ = array_to_tensor(diff)
    return (torch_matmul_to_array(diff_, diff_) + np.trace(sigma1) + np.trace(sigma2) - 2 * tr_covmean)


def calculate_activation_statistics(files, model, batch_size=50, dims=2048,
                                    device='cpu', num_workers=1):
    """Calculation of the statistics used by the FID.
    Params:
    -- files       : List of image files paths
    -- model       : Instance of inception model
    -- batch_size  : The images numpy array is split into batches with
                     batch size batch_size. A reasonable batch size
                     depends on the hardware.
    -- dims        : Dimensionality of features returned by Inception
    -- device      : Device to run calculations
    -- num_workers : Number of parallel dataloader workers

    Returns:
    -- mu    : The mean over samples of the activations of the pool_3 layer of
               the inception model.
    -- sigma : The covariance matrix of the activations of the pool_3 layer of
               the inception model.
    """
    act = get_activations(files, model, batch_size, dims, device, num_workers)
    mu = np.mean(act, axis=0)
    sigma = np.cov(act, rowvar=False)
    return mu, sigma


def compute_statistics(images, model, batch_size, dims, device,
                               num_workers=1):
    m, s = calculate_activation_statistics(images, model, batch_size, dims, device, num_workers)

    return m, s


def calculate_fid(ref, test, batch_size, device, dims=2048, num_workers=1, cache_path=None):
    """
    Calculates the Frechet Inception Distance (FID) between two sets of images.

    Args:
        ref: Path to the directory of reference images or a PyTorch DataLoader/Generator for reference images.
        test: Path to the directory of test images or a PyTorch DataLoader/Generator for test images.
        batch_size (int): Batch size to use for processing images.
        device (torch.device or str): Device to use for computation (e.g., 'cuda', 'cpu').
        dims (int): Dimensionality of Inception features to use. Default is 2048.
        num_workers (int): Number of worker processes to use for data loading. Default is 1.
        cache_path (str, optional): Path to a .pt file to cache/load pre-computed
                                    statistics for the reference dataset. If None,
                                    statistics are always recomputed. If the file
                                    exists, statistics are loaded. If loading fails
                                    or the file doesn't exist, statistics are computed
                                    and saved to this path. Default is None.

    Returns:
        float: The calculated FID score.
    """
    block_idx = InceptionV3.BLOCK_INDEX_BY_DIM[dims]
    model = InceptionV3([block_idx]).to(device)
    model.eval() # Ensure model is in evaluation mode

    m1, s1 = None, None
    if cache_path is not None:
        try:
            print(f"Attempting to load cached reference statistics from: {cache_path}")
            m1, s1 = torch.load(cache_path, map_location=device)
            print("Successfully loaded cached reference statistics.")
        except FileNotFoundError:
            print(f"Cache file not found: {cache_path}. Computing reference statistics.")
            m1, s1 = compute_statistics(ref, model, batch_size, dims, device, num_workers)
            try:
                print(f"Saving computed reference statistics to: {cache_path}")
                torch.save((m1, s1), cache_path)
                print("Successfully saved reference statistics to cache.")
            except Exception as e:
                print(f"Warning: Could not save reference statistics to cache: {cache_path}. Error: {e}")
        except Exception as e:
            print(f"Failed to load cached reference statistics from: {cache_path}. Error: {e}")
            print("Recomputing reference statistics.")
            m1, s1 = compute_statistics(ref, model, batch_size, dims, device, num_workers)
            try:
                print(f"Saving recomputed reference statistics to: {cache_path}")
                torch.save((m1, s1), cache_path)
                print("Successfully saved recomputed reference statistics to cache.")
            except Exception as e_save:
                print(f"Warning: Could not save recomputed reference statistics to cache: {cache_path}. Error: {e_save}")
    else:
        print("No cache path provided. Computing reference statistics.")
        m1, s1 = compute_statistics(ref, model, batch_size, dims, device, num_workers)

    print("Computing test statistics.")
    m2, s2 = compute_statistics(test, model, batch_size,
                                dims, device, num_workers)

    print("Calculating FID value.")
    fid_value = calculate_frechet_distance(m1, s1, m2, s2, device=device) # Removed device argument if not used by actual function

    return fid_value

