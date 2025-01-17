# import libraries
import torch
import numpy as np
from torch.autograd import Variable
import torch.nn as nn
from torchvision.utils import save_image

import os

from argparse import ArgumentParser

from data import create_loaders

# Import the models
from models import Discriminator, Generator
from utils import combine_images_maps

parser = ArgumentParser()
parser.add_argument("--epochs", type=int, default=50, help="number of epochs of training")
parser.add_argument("--grad_updates", type=int, default=1, help="number of training steps for "
                                                                "discriminator per iter")

parser.add_argument('--data', type=str, default='../train_data.npy', help='Training data path')
parser.add_argument('--train-batch-size', type=int, default=32, help='Training batch size')
parser.add_argument('--test-batch-size', type=int, default=64, help='Testing batch size')
parser.add_argument('--loader-threads', type=int, default=4,
                    help='Number of threads of the data loader')
parser.add_argument("--target_set", type=str, default='D', help="which split to remove")
args = parser.parse_args()

# set important training parameters
# generator
lr_gen = 0.0001
betas_gen = (0.5, 0.999)
# discriminator
lr_dis = 0.0001
betas_dis = (0.5, 0.999)
# training
epochs = 50
noise_dim = 128
lambda_gp = 10
sample_interval = 50000
multi_gpu = True

# Check for gpu
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# Create folders to put result files and checkpoints
exp_folder = "exp_" + args.target_set
os.makedirs("./exps/" + exp_folder, exist_ok=True)
os.makedirs("./checkpoints/", exist_ok=True)
os.makedirs("./temp/", exist_ok=True)

# Initialize generator and discriminator and their optimizers
generator = Generator()
opti_gen = torch.optim.Adam(generator.parameters(), lr=lr_gen, betas=betas_gen)

discriminator = Discriminator()
opti_dis = torch.optim.Adam(discriminator.parameters(), lr=lr_dis, betas=betas_dis)

Tensor = torch.FloatTensor

if device.type == "cuda":
    generator.cuda()
    discriminator.cuda()
    Tensor = torch.cuda.FloatTensor
    
def graph_scatter(inputs, device_ids, indices):
    nds_to_sample, eds_to_sample = indices

    # new implementation
    batch_size = (torch.max(nds_to_sample) + 1).detach()
    N = len(device_ids)
    shift = torch.round(torch.linspace(0, batch_size, N+1))[:-1]
    shift = torch.cat((shift,torch.Tensor([batch_size])))

    outputs = []
    for i in range(len(device_ids)):
        if len(inputs) <= 3:
            x, y, z = inputs
        else:
            x, y, z, w = inputs
        inds = torch.where((nds_to_sample >= shift[i]) & (nds_to_sample < shift[i+1]))[0]
        x_split = x[inds]
        y_split = y[inds]
        inds = torch.where(nds_to_sample<shift[i])[0]
        min_val = inds.size(0)
        inds = torch.where((eds_to_sample>=shift[i])&(eds_to_sample<shift[i+1]))[0]
        z_split = z[inds].clone()
        z_split[:, 0] -= min_val
        z_split[:, 2] -= min_val
        if len(inputs) > 3:
            inds = torch.where((nds_to_sample>=shift[i])&(nds_to_sample<shift[i+1]))[0]
            w_split = (w[inds]-shift[i]).long()
            _out = (x_split.to(device_ids[i]),
                    y_split.to(device_ids[i]),
                    z_split.to(device_ids[i]),
                    w_split.to(device_ids[i]))
        else:
            _out = (x_split.to(device_ids[i]),
                    y_split.to(device_ids[i]),
                    z_split.to(device_ids[i]))
        outputs.append(_out)
    return outputs


def data_parallel(module, _input, indices):
    device_ids = list(range(torch.cuda.device_count()))
    output_device = device_ids[0]
    replicas = nn.parallel.replicate(module, device_ids)
    inputs = graph_scatter(_input, device_ids, indices)
    replicas = replicas[:len(inputs)]
    outputs = nn.parallel.parallel_apply(replicas, inputs)
    return nn.parallel.gather(outputs, output_device)


# Visualize a single batch
def visualizeSingleBatch(test_loader, args, batches_done):
    with torch.no_grad():
        # Unpack batch
        masks, nds, eds, nds_to_sample, eds_to_sample = next(iter(test_loader))
        real_masks = Variable(masks.type(Tensor))
        given_nds = Variable(nds.type(Tensor))
        given_eds = eds

        # Generate a batch of images
        z = Variable(Tensor(np.random.normal(0, 1, (given_nds.shape[0], noise_dim))))
        gen_masks = generator(z, given_nds, given_eds)

        # Generate image tensors
        real_imgs_tensor = combine_images_maps(real_masks, given_nds, given_eds,
                                               nds_to_sample, eds_to_sample)
        fake_imgs_tensor = combine_images_maps(gen_masks, given_nds, given_eds,
                                               nds_to_sample, eds_to_sample)

        # Save images
        save_image(real_imgs_tensor, "./exps/{}/{}_real.png".format(exp_folder, batches_done),
                   nrow=12, normalize=False)
        save_image(fake_imgs_tensor, "./exps/{}/{}_fake.png".format(exp_folder, batches_done),
                   nrow=12, normalize=False)
    return


# Load train and test data
train_loader, test_loader = create_loaders(args.data, args.train_batch_size, args.test_batch_size,
                                           args.loader_threads, n_rooms=(10, 12))

def compute_gradient_penalty(dis, real, fake, given_nds=None, given_eds=None, indices=None,
                             data_parallel=None):
    batch_size = torch.max(nds_to_sample) + 1

    alpha = torch.rand((real.shape[0], 1, 1)).to(device)

    x_both = real.data * alpha + fake.data * (1 - alpha)
    x_both = x_both.to(device)
    x_both = Variable(x_both, requires_grad=True)
    grad_outputs = torch.ones(batch_size, 1).to(device)

    if data_parallel:
        dis_out = data_parallel(dis, (x_both, given_nds, given_eds, indices[0]), indices)
    else:
        dis_out = dis(x_both, given_nds, given_eds, indices[0])
    grad = torch.autograd.grad(outputs=dis_out,
                               inputs=x_both,
                               grad_outputs=grad_outputs,
                               retain_graph=True,
                               create_graph=True,
                               only_inputs=True)[0]

    gradient = grad.view(grad.shape[0], -1)
    gradient_norm = gradient.norm(2, dim=1)
    gradient_penalty = torch.mean((gradient_norm - 1) ** 2)
    
    return gradient_penalty

# Training
gen_loss_array = []
disc_loss_array = []
for epoch in range(args.epochs):
    for idx, minibatch in enumerate(train_loader):
        # Unpack the minibatch
        masks, nds, eds, nds_to_sample, eds_to_sample = minibatch
        indices = nds_to_sample, eds_to_sample

        real_masks = Variable(masks.type(Tensor))
        given_nds = Variable(nds.type(Tensor))
        given_eds = eds

        # Train the discriminator
        # Set grads on
        for p in discriminator.parameters():
            p.requires_grad = True
        opti_dis.zero_grad()

        # Generate masks for each room
        z = Variable(Tensor(np.random.normal(0, 1, (given_nds.shape[0], noise_dim))))
        # if multi_gpu:
        if multi_gpu:
            gen_masks = data_parallel(generator, (z, given_nds, given_eds), indices)
        else:
            gen_masks = generator(z, given_nds, given_eds)

        # Evaluate with discriminator
        # Real masks
        if multi_gpu:
            eval_real = data_parallel(discriminator,
                                          (real_masks, given_nds,
                                           given_eds, nds_to_sample), indices)
        else:
            eval_real = discriminator(real_masks, given_nds, given_eds, nds_to_sample)

        # Generated images
        if multi_gpu:
            pass
            eval_fake = data_parallel(discriminator,
                                          (gen_masks.detach(), given_nds.detach(),
                                           given_eds.detach(), nds_to_sample.detach()), indices)
        else:
            eval_fake = discriminator(gen_masks.detach(), given_nds.detach(), given_eds.detach(),
                                      nds_to_sample.detach())

        # Measure the discriminator gradient penalty
        if multi_gpu:
            gradient_penalty = compute_gradient_penalty(discriminator, real_masks.data,
                                                        gen_masks.data,
                                                        given_nds=given_nds.data,
                                                        given_eds=given_eds.data,
                                                        indices=indices,
                                                        data_parallel=data_parallel)
        else:
            gradient_penalty = compute_gradient_penalty(discriminator, real_masks.data,
                                                        gen_masks.data,
                                                        given_nds=given_nds.data,
                                                        given_eds=given_eds.data,
                                                        indices=indices)

        # Compute the discriminator loss with gradient penalty
        dis_loss = - torch.mean(eval_real) + torch.mean(eval_fake) + lambda_gp * gradient_penalty
        disc_loss_array.append(dis_loss.item())

        # Update the discriminator weights and perform one step in the optimizer
        dis_loss.backward()
        opti_dis.step()

        # Set grads off
        for p in discriminator.parameters():
            p.requires_grad = False

        # ==========================================================================================
        # Train the generator
        # ==========================================================================================
        opti_gen.zero_grad()

        # Score fake images
        if multi_gpu:
            eval_fake = data_parallel(discriminator,
                                          (gen_masks, given_nds,
                                           given_eds, nds_to_sample), indices)
        else:
            eval_fake = discriminator(gen_masks, given_nds, given_eds, nds_to_sample)

        # Compute the generator loss
        gen_loss = -torch.mean(eval_fake)
        gen_loss_array.append(gen_loss.item())

        # Update the generator weights and perform one step in the optimizer
        gen_loss.backward()
        opti_gen.step()
        # Save a checkpoint, if the epoch is over
        batches_done = epoch * len(train_loader) + idx
        if idx%500 == 0:
            print("[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f]"
                  % (epoch, args.epochs, idx, len(train_loader), dis_loss.item(), gen_loss.item()))

        if idx%3000 == 0 and idx != 0:
            visualizeSingleBatch(test_loader, args, batches_done)


        if (batches_done % sample_interval == 0):
            torch.save({'gen_state_dict': generator.state_dict(),
                        'disc_state_dict': discriminator.state_dict(),
                        'gen_loss': gen_loss_array,
                        'disc_loss': disc_loss_array,
                        }, './checkpoints/{}_{}.pth'.format(exp_folder, batches_done))
            visualizeSingleBatch(test_loader, args, batches_done)
        batches_done += args.grad_updates

torch.save({'gen_state_dict': generator.state_dict(),
            'disc_state_dict': discriminator.state_dict(),
            'gen_loss': gen_loss_array,
            'disc_loss': disc_loss_array,
            }, './checkpoints/{}_{}.pth'.format(exp_folder, batches_done))