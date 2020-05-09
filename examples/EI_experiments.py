#%%
# Import packages
import torch
import torch.nn as nn
from openmmtools.constants import kB
from simtk import openmm as mm
from simtk import unit
from simtk.openmm import app
from openmmtools.testsystems import AlanineDipeptideImplicit
import numpy as np
import sys
import normflow as nf
sys.path.append('../')
import boltzgen.openmm_interface as omi
import boltzgen.mixed as mixed
from boltzgen.distributions import Boltzmann
from boltzgen.flows import CoordinateTransform
import mdtraj
from matplotlib import pyplot as plt
from tqdm import tqdm

# Set up simulation object
temperature = 1000
kT = kB * temperature

testsystem = AlanineDipeptideImplicit()
implicit_sim = app.Simulation(testsystem.topology,
                              testsystem.system,
                              mm.LangevinIntegrator(temperature * unit.kelvin , 1.0 / unit.picosecond, 1.0 * unit.femtosecond),
                              platform=mm.Platform.getPlatformByName('CPU')
                              )
implicit_sim.context.setPositions(testsystem.positions)

openmm_energy = omi.OpenMMEnergyInterface.apply

# Load the training data 
aldp_traj = mdtraj.load('saved_data/aldp_training_data.h5')

z = [
    (1, [4, 5, 6]),
    (0, [1, 4, 5]),
    (2, [1, 0, 4]),
    (3, [1, 0, 2]),
    (7, [6, 4, 5]),
    (9, [8, 6, 5]),
    (10, [8, 6, 9]),
    (11, [10, 8, 5]),
    (12, [10, 8, 11]),
    (13, [10, 11, 12]),
    (17, [16, 14, 15]),
    (19, [18, 16, 17]),
    (20, [18, 19, 16]),
    (21, [18, 19, 20])
]
backbone_indices = [4, 5, 6, 8, 14, 15, 16, 18]
aldp_traj.center_coordinates()
ind = aldp_traj.top.select("backbone")
aldp_traj.superpose(aldp_traj, 0, atom_indices=ind, ref_atom_indices=ind)

training_data = aldp_traj.xyz
n_atoms = training_data.shape[1]
n_dim = n_atoms * 3
training_data_npy = training_data.reshape(-1, n_dim)
training_data = torch.from_numpy(training_data_npy)
training_data = training_data.double()

mixed_transform = mixed.MixedTransform(66, backbone_indices, z, training_data)
x, _ = mixed_transform.forward(training_data)
print("Training data shape", x.shape)

length_chains = 100
dims = 60

class HMC(nn.Module):
    def __init__(self):
        super().__init__()
        eps = torch.nn.Parameter(0.01 * torch.ones((length_chains, dims), requires_grad=True))
        log_mass = torch.nn.Parameter(0.0 * torch.ones((length_chains, dims), requires_grad=True))
        self.register_parameter('eps', eps)
        self.register_parameter('log_mass', log_mass)

    def logP(self, x):
        z, invlogdet = mixed_transform.inverse(x)
        invlogdet = invlogdet.view((invlogdet.shape[0], 1))
        raw_energy = openmm_energy(z, implicit_sim.context, temperature)
        reg_energy = omi.regularize_energy(raw_energy,
            torch.tensor(1e3), torch.tensor(1e8))
        return -reg_energy + invlogdet

    def gradlogP(self, x):
        xp = torch.tensor(x, requires_grad=True)
        assert xp.grad_fn is None
        z = self.logP(xp)
        z.backward(torch.ones((xp.shape[0],1)))
        return xp.grad

    def leapfrog(self, x, p, eps, log_mass):
        # x is pos
        # p is momentum
        for i in range(10):
            p_half = p - (eps / 2.0) * -self.gradlogP(x)
            x = x + eps * (p_half/torch.exp(log_mass))
            p = p_half - (eps / 2.0) * -self.gradlogP(x)
        return x, p

    def draw_samples(self, initial_samples, L=length_chains, return_probs=False):
        x = initial_samples

        all_probs = np.zeros((L, initial_samples.shape[0]))
        for i in range(L):
            # Draw momentum
            p = torch.randn_like(x) * torch.exp(0.5 * self.log_mass[i, :])
            # Propose new states
            x_new, p_new = self.leapfrog(x, p, self.eps[i, :], self.log_mass[i, :])

            # Apply M_H
            probs = torch.exp(self.logP(x_new)[:,0] - self.logP(x)[:,0] - \
                0.5*torch.sum(p_new**2 / torch.exp(self.log_mass[i, :]), 1) + \
                0.5*torch.sum(p**2 / torch.exp(self.log_mass[i, :]), 1))
            uniforms = torch.rand_like(probs)
            mask = (uniforms < probs).int()
            mask = torch.transpose(torch.stack(tuple([mask for i in range(60)])), 0, 1)
            x = x_new * mask + x * (1-mask)

            all_probs[i, :] = probs.detach().numpy()

        if return_probs:
            return x, all_probs
        else:
            return x

    def get_log_target_graph(self, initial_samples):
        x = initial_samples
        log_targets = []
        for i in range(length_chains):
            # Draw momentum
            p = torch.randn_like(x) * torch.exp(0.5 * self.log_mass[i, :])
            # Propose new states
            x_new, p_new = self.leapfrog(x, p, self.eps[i, :], self.log_mass[i, :])

            # Apply M_H
            probs = torch.exp(self.logP(x_new)[:,0] - self.logP(x)[:,0] - \
                0.5*torch.sum(p_new**2 / torch.exp(self.log_mass[i, :]), 1) + \
                0.5*torch.sum(p**2 / torch.exp(self.log_mass[i, :]), 1))
            uniforms = torch.rand_like(probs)
            mask = (uniforms < probs).int()
            mask = torch.transpose(torch.stack(tuple([mask for i in range(60)])), 0, 1)
            x = x_new * mask + x * (1-mask)
            log_target = -torch.mean(self.logP(x))
            log_targets.append(log_target.detach().numpy())
            if i % 10 == 0:
                print(i)
        return np.array(log_targets)


    def forward(self, initial_samples, L=length_chains):
        samples = self.draw_samples(initial_samples, L)
        return -torch.mean(self.logP(samples))

hmc = HMC()


# Define flows
K = 8
#torch.manual_seed(0)

latent_size = 60
b = torch.Tensor([1 if i % 2 == 0 else 0 for i in range(latent_size)])
flows = []
for i in range(K):
    s = nf.nets.MLP([latent_size, 4 * latent_size,
        4 * latent_size, latent_size], output_fn='tanh', output_scale=3.0)
    t = nf.nets.MLP([latent_size, 4 * latent_size,
        4 * latent_size, latent_size])
    if i % 2 == 0:
        flows += [nf.flows.MaskedAffineFlow(b, s, t)]
    else:
        flows += [nf.flows.MaskedAffineFlow(1 - b, s, t)]
    flows += [nf.flows.ActNorm(latent_size)]
flows += [CoordinateTransform(training_data, 66, z, backbone_indices)]

# Set prior and q0
prior = Boltzmann(implicit_sim.context, temperature, energy_cut=1e3,
    energy_max=1e20)
q0 = nf.distributions.DiagGaussian(latent_size)

# Construct flow model
nfm = nf.NormalizingFlow(q0=q0, flows=flows, p=prior)

# Move model on GPU if available
enable_cuda = True
device = torch.device('cuda' if torch.cuda.is_available() and enable_cuda else 'cpu')
nfm = nfm.to(device)
nfm = nfm.double()

nfm.load_state_dict(torch.load('models/flow_iter_50000'))

def fix_dih(x):
    x = np.where(x<-np.pi, x+2*np.pi, x)
    x = np.where(x>np.pi, x-2*np.pi, x)
    return x

#%%
# ----------- Train Flow ---------------
batch_size = 128
ml_losses = np.array([])
optimizer = torch.optim.Adam(nfm.parameters(), lr=1e-4, weight_decay=1e-3)
for iter in tqdm(range(21000, 100000)):
    optimizer.zero_grad()
    ind = torch.randint(training_data.shape[0], (batch_size,))
    mini_batch = training_data[ind, :].double()
    ml_loss = nfm.forward_kld(mini_batch)
    ml_loss.backward()
    optimizer.step()
    

    ml_losses = np.append(ml_losses, ml_loss.detach().numpy())
    if iter % 1000 == 0:
        plt.plot(ml_losses)
        plt.ylim([-340, -290])
        plt.grid(True)
        plt.show()

    if iter % 5000 == 0:
        torch.save(nfm.state_dict(), 'models/flow_iter_{}'.format(iter))

#%%
# ---------- Test flow marginals ---------
flow_samples, _ = nfm.sample(10000)
flow_samples, _ = flows[-1].inverse(flow_samples)
flow_samples = flow_samples.detach().numpy()
x_np = x.detach().numpy()
import scipy.stats
for i in range(60):
    kde_flow = scipy.stats.gaussian_kde(flow_samples[:, i])
    kde_data = scipy.stats.gaussian_kde(x_np[:, i])
    positions = np.linspace(-10, 10, 1000)
    plt.plot(positions, kde_data(positions))
    plt.plot(positions, kde_flow(positions))
    plt.show()

# %%