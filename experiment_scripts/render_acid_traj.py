"""
checkpoint (*WITH SOFTRAS SPLIT*) under 
/om2/user/sitzmann/logs/light_fields/NMR_hyper_1e2_reg_layernorm/64_256_None/checkpoints/model_epoch_0087_iter_250000.pth
"""

# Enable import from parent package
import sys
import os
sys.path.append( os.path.dirname( os.path.dirname( os.path.abspath(__file__) ) ) )
import random

import torch
import numpy as np
# torch.multiprocessing.set_sharing_strategy('file_system')
# torch.multiprocessing.set_start_method('spawn', force=True)
import torch.distributed as dist
import torch.multiprocessing as mp
from acid_dataio import ACID, get_camera_pose
# from realestate10k_dataio import RealEstate10k, get_camera_pose
# from multiprocessing import Manager
import torch
import models
import training
import configargparse
from torch.utils.data import DataLoader
import loss_functions
import summaries
import util
import config
from tqdm import tqdm
from imageio import imwrite, get_writer
from glob import glob

# torch.manual_seed(0)

p = configargparse.ArgumentParser()
p.add('-c', '--config_filepath', required=False, is_config_file=True)

p.add_argument('--logging_root', type=str, default=config.logging_root)
p.add_argument('--data_root', type=str, default='/om2/user/egger/MultiClassSRN/data/NMR_Dataset', required=False)
p.add_argument('--val_root', type=str, default=None, required=False)
p.add_argument('--network', type=str, default='relu')
p.add_argument('--category', type=str, default='donut')
p.add_argument('--conditioning', type=str, default='hyper')
p.add_argument('--experiment_name', type=str, required=True)
p.add_argument('--num_context', type=int, default=0)
p.add_argument('--batch_size', type=int, default=48)
p.add_argument('--max_num_instances', type=int, default=None)
p.add_argument('--num_trgt', type=int, default=1)
p.add_argument('--gpus', type=int, default=1)
p.add_argument('--views', type=int, default=1)

# General training options
p.add_argument('--lr', type=float, default=5e-4)
p.add_argument('--num_epochs', type=int, default=40001)
p.add_argument('--reconstruct', action='store_true', default=False)
p.add_argument('--nearest', action='store_true', default=False)
p.add_argument('--local', action='store_true', default=False)
p.add_argument('--local_coord', action='store_true', default=False)
p.add_argument('--learned_local_coord', action='store_true', default=False)
p.add_argument('--global_local_coord', action='store_true', default=False)
p.add_argument('--model', type=str, default='midas_vit')
p.add_argument('--autodecoder', action='store_true', default=False)
p.add_argument('--epochs_til_ckpt', type=int, default=10)
p.add_argument('--steps_til_summary', type=int, default=500)
p.add_argument('--iters_til_ckpt', type=int, default=10000)
p.add_argument('--checkpoint_path', default=None)
opt = p.parse_args()


def sync_model(model):
    for param in model.parameters():
        dist.broadcast(param.data, 0)

def worker_init_fn(worker_id):
    random.seed(int(torch.utils.data.get_worker_info().seed)%(2**32-1))
    np.random.seed(int(torch.utils.data.get_worker_info().seed)%(2**32-1))


def render_data(model_input, scene, model):
    model_input = util.dict_to_gpu(model_input)

    nrender = model_input['query']['cam2world'].size(1)
    query_cam2world = model_input['query']['cam2world']

    context_cam2world = model_input['context']['cam2world']
    # dist = torch.norm(context_cam2world[0, 0, :3, -1] - context_cam2world[0, 1, :3, -1], p=2, dim=-1)
    # import pdb
    # pdb.set_trace()
    # print(model_input['context']['intrinsics'])
    # print("distance: ", dist)

    # return


    query_intrinsic = model_input['query']['intrinsics']
    uv = model_input['query']['uv']
    nrays = uv.size(-2)
    chunks = nrays // 8192 + 1
    z = model.get_z(model_input)

    query_cam2world = model_input['query']['cam2world']
    query_intrinsic = model_input['query']['intrinsics']

    scene = str(scene)
    scene_path = scene.split("/")[-1]
    # writer = get_writer("cvpr_acid/{}.mp4".format(scene_path))
    writer = get_writer("render_videos_acid/{}.mp4".format(scene_path))
    gt_writer = get_writer("render_videos_acid/{}_gt.mp4".format(scene_path))

    with torch.no_grad():
        for i in tqdm(range(nrender)):
            model_input['query']['cam2world'] = query_cam2world[:, i:i+1]
            model_input['query']['intrinsics'] = query_intrinsic[:, i:i+1]
            # import pdb
            # pdb.set_trace()
            # print(model_input['context'])

            uv_i = uv[:, i:i+1]
            # model_input['query']['uv'] = uv[:, i:i+1]

            uv_chunks = torch.chunk(uv_i, chunks, dim=2)

            model_outputs = []

            for uv_chunk in uv_chunks:
                model_input['query']['uv'] = uv_chunk
                model_output = model(model_input, z=z)
                del model_output['z']
                del model_output['coords']
                # del model_output['ent']
                del model_output['uv']
                del model_output['pixel_val']
                del model_output['at_wts']

                model_outputs.append(model_output)

            model_output_full = {}

            for k in model_outputs[0].keys():
                outputs = [model_output[k] for model_output in model_outputs]
                print(k, [output.size() for output in outputs])
                val = torch.cat(outputs, dim=-2)
                model_output_full[k] = val

            rgb = model_output_full['rgb'].view(256, 256, 3)
            rgb_gt = model_input['query']['rgb'][0, i]
            rgb = (rgb + 1) / 2.
            rgb_gt = (rgb_gt + 1) / 2.
            rgb = torch.clamp(rgb, 0, 1)
            rgb_gt = torch.clamp(rgb_gt, 0, 1)

            rgb = (rgb.detach().cpu().numpy() * 255).astype(np.uint8)
            rgb_gt = (rgb_gt.detach().cpu().numpy() * 255).astype(np.uint8)

            writer.append_data(rgb)
            gt_writer.append_data(rgb_gt)

            # panel_im = torch.cat([rgb, rgb_gt], dim=1).detach().cpu().numpy()
            # panel_im = np.clip(panel_im, -1, 1)
            # writer.append_data(panel_im)

    writer.close()
    gt_writer.close()

    pass

def multigpu_train(gpu, opt):
    if opt.gpus > 1:
        dist.init_process_group(backend='nccl', init_method='tcp://localhost:1492', world_size=opt.gpus, rank=gpu)

    torch.cuda.set_device(gpu)

    train_dataset = ACID(img_root="/nobackup/projects/public/RealEstate10k/ACID/dataset_hi_res/train",
                                 pose_root="/nobackup/projects/public/RealEstate10k/ACID/ACID/train.mat",
                                 num_ctxt_views=1, num_query_views=1, query_sparsity=256,
                                 )

    test_dataset = ACID(img_root="/nobackup/projects/public/RealEstate10k/ACID/dataset_hi_res/test",
                                 pose_root="/nobackup/projects/public/RealEstate10k/ACID/ACID/test.mat",
                                 num_ctxt_views=1, num_query_views=1, query_sparsity=256,
                                 )

    # Train ids: 6, 19, 20 [good], 21, 22, 23, 25 [decent], 26, 27
    # Test ids: 27, 0, 1 [decent], 3 [bad synthesis, good trajectory], 5 [decent], 8, 9 [ok], 11 [bad], 13 [decent], 15[decent], 18[bad], 19[out of context], 23 [good], 26[decent], 29[drifting out], 31, 33, 35, 39 [good], 48 [too far apart]
    # 50 is forest scene, 52
    # 23, 15, 5
    # Good dual scenes 39, 13
    # 15 has disocclusion
    train_scenes = [str(s).split("/")[-1] for s in train_dataset.all_scenes]
    scenes = [s.split("/")[-1][:-4] for s in glob("/home/yilundu/*.mp4")]

    model = models.CrossAttentionRenderer(local_coord=opt.local_coord, learned_local_coord=opt.learned_local_coord, global_local_coord=opt.global_local_coord, nearest=opt.nearest, model=opt.model, n_view=opt.views)
    old_state_dict = model.state_dict()

    if opt.checkpoint_path is not None:
        print(f"Loading weights from {opt.checkpoint_path}...")
        state_dict = torch.load(opt.checkpoint_path, map_location=torch.device('cpu'))
        if opt.reconstruct:
            state_dict['latent_codes.weight'] = torch.zeros_like(state_dict['latent_codes.weight'])

        # state_dict['encoder.latent'] = old_state_dict['encoder.latent']

        model.load_state_dict(state_dict['model'], strict=not opt.reconstruct)

    model = model.cuda()

    # for idx in [31, 50]:
    for idx in [50]:
        # if scene not in test_scenes:
        #     continue

        # idx = test_scenes.index(scene)
        try:
            all_scene = test_dataset.all_scenes[idx]
            data = get_camera_pose(all_scene, "/nobackup/projects/public/RealEstate10k/ACID/ACID/test", test_dataset.uv, views=opt.views)

        except:
            continue

        render_data(data, all_scene, model)

if __name__ == "__main__":
    # manager = Manager()
    # shared_dict = manager.dict()

    opt = p.parse_args()
    if opt.gpus > 1:
        mp.spawn(multigpu_train, nprocs=opt.gpus, args=(opt,))
    else:
        multigpu_train(0, opt)
