from . import mixed
import normflow as nf
import numpy as np
import torch

class CoordinateTransform(nf.flows.Flow):
    """
    Coordinate transform for Boltzmann generators, see
    https://science.sciencemag.org/content/365/6457/eaaw1147

    Meaning of forward and backward pass are switched to meet
    convention of normflow package
    """
    def __init__(self, data, n_dim, z_matrix, backbone_indices):
        """
        Constructor
        :param data: Data used to initialize transformation
        :param n_dim: Number of dimensions in original space
        :param z_matrix: Defines which atoms to represent in internal coordinates
        :param backbone_indices: Indices of atoms of backbone, will be left in
        cartesian coordinates
        """
        super().__init__()
        self.mixed_transform = mixed.MixedTransform(n_dim, backbone_indices, z_matrix, data)

    def forward(self, z):
        z_, log_det = self.mixed_transform.inverse(z)
        return z_, log_det

    def inverse(self, z):
        z_, log_det = self.mixed_transform.forward(z)
        return z_, log_det

class Scaling(nf.flows.Flow):
    """
    Applys a scaling factor
    """
    def __init__(self, mean, scale):
        """
        Constructor
        :param means: The mean of the previous layer
        :param scale: scale factor to apply
        """
        super().__init__()
        self.register_buffer('mean', mean)
        self.register_buffer('scale', scale)

    def forward(self, z):
        z_ = (z-self.mean) * self.scale + self.mean
        logdet = np.log(self.scale) * self.mean.shape[0]
        return z_, logdet

    def inverse(self, z):
        z_ = (z-self.mean) / self.scale + self.mean
        logdet = -np.log(self.scale) * self.mean.shape[0]
        return z_, logdet

class AddNoise(nf.flows.Flow):
    """
    Adds a small amount of Gaussian noise
    """
    def __init__(self, std):
        """
        Constructor
        :param std: The standard deviation of the noise
        """
        super().__init__()
        self.register_buffer('noise_std', std)

    def forward(self, z):
        eps = torch.randn_like(z)
        z_ = z + self.noise_std * eps
        logdet = torch.zeros(z_.shape[0])
        return z_, logdet

    def inverse(self, z):
        return self.forward(z)
