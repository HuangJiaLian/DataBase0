
import os
import sys
import time
import copy
import yaml
import pickle
import random
import numpy as np
import webdataset as wds
from functools import partial
from pathlib import Path

import torch
from torch import nn, optim
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel
from torch.distributed.algorithms.join import Join
from torch.utils.data.distributed import DistributedSampler

sys.path.append('./ASD-AFM-dev') # Path to ASD-AFM-dev repo
import asdafm.common_utils          as cu
import asdafm.graph.graph_utils     as gu
import asdafm.preprocessing         as pp
import asdafm.data_loading          as dl
import asdafm.visualization         as vis
from asdafm.graph.models            import PosNetAdaptive
from asdafm.parsing_utils           import update_config
from asdafm.logging                 import LossLogPlot, SyncedLoss

def make_model(device, cfg):
    model = PosNetAdaptive(
        encode_block_channels   = [16, 32, 64, 128],
        encode_block_depth      = 3,
        decode_block_channels   = [128, 64, 32],
        decode_block_depth      = 2,
        decode_block_channels2  = [128, 64, 32],
        decode_block_depth2     = 3,
        attention_channels      = [128, 128, 128],
        res_connections         = True,
        activation              = 'relu',
        padding_mode            = 'zeros',
        pool_type               = 'avg',
        decoder_z_sizes         = [5, 15, 35],
        z_outs                  = [3, 3, 5, 10],
        peak_std                = cfg['peak_std']
    ).to(device)
    criterion = nn.MSELoss(reduction='mean')
    optimizer = optim.Adam(model.parameters(), lr=cfg['lr'])
    lr_decay_rate = 1e-5
    lr_decay = optim.lr_scheduler.LambdaLR(optimizer, lambda b: 1.0/(1.0+lr_decay_rate*b))
    return model, criterion, optimizer, lr_decay

def apply_preprocessing(batch, cfg):

    box_res = cfg['box_res']
    z_lims = cfg['z_lims']
    zmin = cfg['zmin']
    peak_std = cfg['peak_std']

    X, atoms, scan_windows = [batch[k] for k in ['X', 'xyz', 'sw']]

    # Pick a random number of slices between 1 and 15 and randomize start slice between 0-4
    nz = random.choice(range(1, 16))
    z0 = random.choice(range(0, min(5, 16-nz)))
    X = [x[:, :, :, -nz:] for x in X] if z0 == 0 else [x[:, :, :, -(nz+z0):-z0] for x in X]

    atoms = [a[a[:, -1] != 79] for a in atoms] # Remove gold atoms
    atoms = pp.top_atom_to_zero(atoms)
    xyz = atoms.copy()
    mols = [gu.MoleculeGraph(a, []) for a in atoms]
    mols, sw = gu.shift_mols_window(mols, scan_windows[0])

    box_borders = (
        (0, 0, z_lims[0]),
        (box_res[0]*(X[0].shape[1] - 1), box_res[1]*(X[0].shape[2] - 1), z_lims[1])
    )
    pp.rand_shift_xy_trend(X, shift_step_max=0.02, max_shift_total=0.04)
    X, mols, box_borders = gu.add_rotation_reflection_graph(X, mols, box_borders, num_rotations=3,
        reflections=True, crop=(128, 128), per_batch_item=True)
    pp.add_norm(X)
    pp.add_gradient(X, c=0.3)
    pp.add_noise(X, c=0.1, randomize_amplitude=True, normal_amplitude=True)
    pp.add_cutout(X, n_holes=5)
    
    mols = gu.threshold_atoms_bonds(mols, zmin)
    ref = gu.make_position_distribution(mols, box_borders, box_res=box_res, std=peak_std)

    return X, [ref], xyz, box_borders

def make_webDataloader(cfg, mode='train'):
    
    assert mode in ['train', 'val', 'test'], mode

    shard_list = os.path.join(cfg['data_dir'], cfg['urls'][mode])
    split_pos = shard_list.find('::')
    if split_pos > 0:
        split_pos += 2
        shard_list = shard_list[:split_pos] + os.path.join(cfg['data_dir'], shard_list[split_pos:])
    apply_preprocessing_ = partial(apply_preprocessing, cfg=cfg)

    dataset = wds.WebDataset(dl.ShardList(shard_list, world_size=cfg['world_size'], rank=cfg['global_rank'],
        substitute_param=(mode == 'train'), log=Path(cfg['run_dir']) / 'shards.log'))
    dataset.pipeline.pop()
    if mode == 'train': dataset.append(wds.shuffle(10))     # Shuffle order of shards
    dataset.append(wds.tariterators.tarfile_to_samples())   # Gather files inside tar files into samples
    dataset.append(wds.split_by_worker)                     # Use a different subset of samples in shards in different workers
    if mode == 'train': dataset.append(wds.shuffle(100))    # Shuffle samples within a worker process
    dataset.append(wds.decode('pill', dl.decode_xyz))       # Decode image and xyz files
    dataset.append(dl.rotate_and_stack(reverse=False))      # Combine separate images into a stack, reverse=True only for QUAM dataset
    dataset.append(dl.batched(cfg['batch_size']))           # Gather samples into batches
    dataset = dataset.map(apply_preprocessing_)             # Preprocess

    dataloader = wds.WebLoader(dataset, num_workers=cfg['num_workers'], batch_size=None, pin_memory=True,
        collate_fn=dl.default_collate, persistent_workers=True)
    
    return dataset, dataloader

def batch_to_device(batch, device):
    X, ref, *rest = batch
    X = X[0].to(device)
    ref = ref[0].to(device)
    return X, ref, *rest

def batch_to_host(batch):
    X, ref, pred, xyz = batch
    X = X.squeeze(1).cpu()
    ref = ref.cpu()
    pred = pred.cpu()
    return X, ref, pred, xyz

def run(cfg):

    # Initialize the distributed environment.
    dist.init_process_group(cfg['comm_backend'])

    start_time = time.perf_counter()

    if cfg['global_rank'] == 0:
        # Create run directory
        if not os.path.exists(cfg['run_dir']):
            os.makedirs(cfg['run_dir'])
    
    # Define model, optimizer, and loss
    model, criterion, optimizer, lr_decay = make_model(cfg['local_rank'], cfg)
    
    if cfg['global_rank'] == 0:
        print(f'World size = {cfg["world_size"]}')
        print(f'Trainable parameters: {cu.count_parameters(model)}')

    # Setup checkpointing and load a checkpoint if available
    checkpointer = cu.Checkpointer(model, optimizer, additional_data={'lr_params': lr_decay},
        checkpoint_dir=os.path.join(cfg['run_dir'], 'Checkpoints/'), keep_last_epoch=True)
    init_epoch = checkpointer.epoch
    
    # Setup logging
    log_file = open(cfg['batch_log_path'], 'a')
    loss_logger = LossLogPlot(
        log_path=os.path.join(cfg['run_dir'], 'loss_log.csv'),
        plot_path=os.path.join(cfg['run_dir'], 'loss_history.png'),
        loss_labels=cfg['loss_labels'],
        loss_weights=cfg['loss_weights'],
        print_interval=cfg['print_interval'],
        init_epoch=init_epoch,
        stream=log_file
    )

    # Wrap model in DistributedDataParallel.
    model = DistributedDataParallel(model, device_ids=[cfg['local_rank']], find_unused_parameters=False)

    if cfg['train']:

        # Create datasets and dataloaders
        train_set, train_loader = make_webDataloader(cfg, 'train')
        val_set, val_loader = make_webDataloader(cfg, 'val')

        if cfg['global_rank'] == 0:
            if init_epoch <= cfg['epochs']:
                print(f'\n ========= Starting training from epoch {init_epoch}')
            else:
                print('Model already trained')
        
        for epoch in range(init_epoch, cfg['epochs']+1):

            if cfg['global_rank'] == 0: print(f'\n === Epoch {epoch}')

            # Train
            if cfg['timings'] and cfg['global_rank'] == 0: t0 = time.perf_counter()

            model.train()
            with Join([model, loss_logger.get_joinable('train')]):
                for ib, batch in enumerate(train_loader):

                    # Transfer batch to device
                    X, ref, _, _ = batch_to_device(batch, cfg['local_rank'])

                    if cfg['timings'] and cfg['global_rank'] == 0:
                        torch.cuda.synchronize()
                        t1 = time.perf_counter()
                    
                    # Forward
                    pred = model(X)
                    loss = criterion(pred, ref)
                    
                    if cfg['timings'] and cfg['global_rank'] == 0: 
                        torch.cuda.synchronize()
                        t2 = time.perf_counter()
                    
                    # Backward
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    lr_decay.step()

                    # Log losses
                    loss_logger.add_train_loss(loss)

                    if cfg['timings'] and cfg['global_rank'] == 0:
                        torch.cuda.synchronize()
                        t3 = time.perf_counter()
                        print(f'(Train {ib}) Load Batch/Forward/Backward: {t1-t0:6f}/{t2-t1:6f}/{t3-t2:6f}')
                        t0 = t3

            # Validate
            if cfg['global_rank'] == 0:
                val_start = time.perf_counter()
                if cfg['timings']: t0 = val_start
            
            model.eval()
            with Join([loss_logger.get_joinable('val')]):
                with torch.no_grad():
                    
                    for ib, batch in enumerate(val_loader):
                        
                        # Transfer batch to device
                        X, ref, _, _ = batch_to_device(batch, cfg['local_rank'])
                        
                        if cfg['timings'] and cfg['global_rank'] == 0: 
                            torch.cuda.synchronize()
                            t1 = time.perf_counter()
                        
                        # Forward
                        pred = model.module(X)
                        loss = criterion(pred, ref)

                        loss_logger.add_val_loss(loss)
                        
                        if cfg['timings'] and cfg['global_rank'] == 0:
                            torch.cuda.synchronize()
                            t2 = time.perf_counter()
                            print(f'(Val {ib}) Load Batch/Forward: {t1-t0:6f}/{t2-t1:6f}')
                            t0 = t2

            # Write average losses to log and report to terminal
            loss_logger.next_epoch()

            # Save checkpoint
            checkpointer.next_epoch(loss_logger.val_losses[-1][0])
            
    # Return to best epoch, and save model weights
    dist.barrier()
    checkpointer.revert_to_best_epoch()
    if cfg['global_rank'] == 0:
        torch.save(model.module.state_dict(), save_path := os.path.join(cfg['run_dir'], 'best_model.pth'))
        print(f'\nModel saved to {save_path}')
        print(f'Best validation loss on epoch {checkpointer.best_epoch}: {checkpointer.best_loss}')
        print(f'Average of best 10 validation losses: {np.sort(loss_logger.val_losses[:, 0])[:10].mean()}')

    if cfg['test'] or cfg['predict']:
        test_set, test_loader = make_webDataloader(cfg, 'test')

    if cfg['test']:

        if cfg['global_rank'] == 0: print(f'\n ========= Testing with model from epoch {checkpointer.best_epoch}')

        eval_losses = SyncedLoss(len(loss_logger.loss_labels))
        eval_start = time.perf_counter()
        if cfg['timings'] and cfg['global_rank'] == 0:
            t0 = eval_start
        
        model.eval()
        with Join([eval_losses]):
            with torch.no_grad():
                
                for ib, batch in enumerate(test_loader):
                    
                    # Transfer batch to device
                    X, ref, _, _ = batch_to_device(batch, cfg['local_rank'])
                    
                    if cfg['timings'] and cfg['global_rank'] == 0:
                        torch.cuda.synchronize()
                        t1 = time.perf_counter()
                    
                    # Forward
                    pred = model(X)
                    loss = criterion(pred, ref)
                    eval_losses.append(loss)

                    if (ib+1) % cfg['print_interval'] == 0 and cfg['global_rank'] == 0:
                        print(f'Test Batch {ib+1}', file=log_file, flush=True)
                    
                    if cfg['timings'] and cfg['global_rank'] == 0:
                        torch.cuda.synchronize()
                        t2 = time.perf_counter()
                        print(f'(Test {ib}) t0/Load Batch/Forward: {t1-t0:6f}/{t2-t1:6f}')
                        t0 = t2

        if cfg['global_rank'] == 0:

            # Average losses and print
            eval_loss = eval_losses.mean()
            print(f'Test set loss: {loss_logger.loss_str(eval_loss)}')

            # Save test set loss to file
            with open(os.path.join(cfg['run_dir'], 'test_loss.txt'),'w') as f:
                f.write(';'.join([str(l) for l in eval_loss]))

    if cfg['predict'] and cfg['global_rank'] == 0:
    
        # Make predictions
        print(f'\n ========= Predict on {cfg["pred_batches"]} batches from the test set')
        counter = 0
        pred_dir = os.path.join(cfg['run_dir'], 'predictions/')
        
        with torch.no_grad():
            
            for ib, batch in enumerate(test_loader):
            
                if ib >= cfg['pred_batches']: break
                
                # Transfer batch to device
                X, ref, xyz, box_borders = batch_to_device(batch, cfg['local_rank'])
                
                # Forward
                pred = model.module(X)
                loss = criterion(pred, ref)

                # Back to host
                X, ref, pred, xyz = batch_to_host((X, ref, pred, xyz))

                # Save xyzs
                cu.batch_write_xyzs(xyz, outdir=pred_dir, start_ind=counter)
            
                # Visualize predictions
                vis.plot_distribution_grid(pred, ref, box_borders=box_borders, outdir=pred_dir,
                    start_ind=counter)
                vis.make_input_plots([X], outdir=pred_dir, start_ind=counter)

                counter += len(X)

    print(f'Done at rank {cfg["global_rank"]}. Total time: {time.perf_counter() - start_time:.0f}s')

    log_file.close()
    dist.barrier()
    dist.destroy_process_group()

if __name__ == '__main__':
    
    # Read config
    with open('./config.yaml', 'r') as f:
        cfg = yaml.safe_load(f)
    cfg = update_config(cfg)
    if not os.path.exists(cfg['run_dir']):
        os.makedirs(cfg['run_dir'])
    with open(os.path.join(cfg['run_dir'], 'config.yaml'), 'w') as f:
        yaml.safe_dump(cfg, f)

    # Set random seeds
    torch.manual_seed(cfg['random_seed'])
    random.seed(cfg['random_seed'])
    np.random.seed(cfg['random_seed'])

    # Start run
    mp.set_start_method('spawn')
    cfg['world_size'] = int(os.environ['WORLD_SIZE'])
    cfg['global_rank'] = int(os.environ['RANK'])
    cfg['local_rank'] = int(os.environ['LOCAL_RANK'])
    run(cfg)
