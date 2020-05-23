import numpy as np
import argparse
import glob
import os
from functools import partial
import vispy
import scipy.misc as misc
from tqdm import tqdm
import yaml
import sys
from mesh import write_ply, read_ply, output_3d_photo
from utils import get_MiDaS_samples, read_MiDaS_depth, sparse_bilateral_filtering
import torch
import cv2
from skimage.transform import resize
import imageio
import copy
from networks import Inpaint_Color_Net, Inpaint_Depth_Net, Inpaint_Edge_Net
from MiDaS.run import run_depth
from MiDaS.monodepth_net import MonoDepthNet
import MiDaS.MiDaS_utils as MiDaS_utils
from universal_analytics import Tracker, HTTPRequest, HTTPBatchRequest

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, default='argument.yml', help='Configure of post processing')
args = parser.parse_args()
config = yaml.load(open(args.config, 'r'))
if config['offscreen_rendering'] is True:
	vispy.use(app='egl')
os.makedirs(config['mesh_folder'], exist_ok=True)
os.makedirs(config['video_folder'], exist_ok=True)
os.makedirs(config['depth_folder'], exist_ok=True)
sample_list = get_MiDaS_samples(config['src_folder'], config['depth_folder'], config, config['specific'])
normal_canvas, all_canvas = None, None

params = {
    "depth_threshold": [0.02, 0.03, 0.04, 0.05, 0.06],
    "ext_edge_threshold": [0.001, 0.002, 0.003],
    "sparse_iter": [1, 2, 3, 4, 5],
    "sigma_s": [2, 3, 4, 5, 6],
    "sigma_r": [2, 0.4, 0.5, 0.6, 0.7],
    "redundant_number": [9, 12, 15],
    "background_thickness": [30, 50, 70, 90, 110],
    "context_thickness": [60, 100, 140, 180, 220],
    "depth_edge_dilate": [4, 7, 10, 13, 16],
    "depth_edge_dilate_2": [1, 3, 5, 7, 9],
    "largest_size": [512, 1024],
    "extrapolate_border": [True, False],
    "extrapolation_thickness": [40, 50, 60, 70, 80],
    "repeat_inpaint_edge": [True, False],
    "repeat_inpaint_edge": [True, False],
}

default_config = config.copy()

with HTTPRequest() as http:
	tracker = Tracker("UA-166882787-1", http, client_id="client-01")

	if isinstance(config["gpu_ids"], int) and (config["gpu_ids"] >= 0):
		device = config["gpu_ids"]
	else:
		device = "cpu"

	print("Pict List => {0}".format(sample_list))

	for idx in tqdm(range(len(sample_list))):

		depth = None
		sample = sample_list[idx]
		print("========  ")
		print("{0}/{1} => {2}".format(idx, len(sample_list), sample['src_pair_name']))

		for param_key in params:
			for param in params[param_key]:
				file_name = "{0}-{1}".format(param_key, param)
				config = default_config.copy()
				config[param_key] = param

				try:

					mesh_fi = os.path.join(config['mesh_folder'], sample['src_pair_name'] + '.ply')
					image = imageio.imread(sample['ref_img_fi'])
					run_depth([sample['ref_img_fi']],
					          config['src_folder'],
					          config['depth_folder'],
					          config['MiDaS_model_ckpt'],
					          MonoDepthNet,
					          MiDaS_utils,
					          target_w=640)
					config['output_h'], config['output_w'] = np.load(sample['depth_fi']).shape[:2]
					frac = config['longer_side_len'] / max(config['output_h'], config['output_w'])
					config['output_h'], config['output_w'] = int(config['output_h'] * frac), int(config['output_w'] * frac)
					config['original_h'], config['original_w'] = config['output_h'], config['output_w']
					if image.ndim == 2:
						image = image[..., None].repeat(3, -1)
					if np.sum(np.abs(image[..., 0] - image[..., 1])) == 0 and np.sum(np.abs(image[..., 1] - image[..., 2])) == 0:
						config['gray_image'] = True
					else:
						config['gray_image'] = False
					image = cv2.resize(image, (config['output_w'], config['output_h']), interpolation=cv2.INTER_AREA)
					depth = read_MiDaS_depth(sample['depth_fi'], 3.0, config['output_h'], config['output_w'])
					mean_loc_depth = depth[depth.shape[0] // 2, depth.shape[1] // 2]
					if not (config['load_ply'] is True and os.path.exists(mesh_fi)):
						vis_photos, vis_depths = sparse_bilateral_filtering(depth.copy(), image.copy(), config, num_iter=config['sparse_iter'], spdb=False)
						depth = vis_depths[-1]
						model = None
						torch.cuda.empty_cache()

						print("Start Running 3D_Photo ...")
						depth_edge_model = Inpaint_Edge_Net(init_weights=True)
						depth_edge_weight = torch.load(config['depth_edge_model_ckpt'], map_location=torch.device(device))
						depth_edge_model.load_state_dict(depth_edge_weight)
						depth_edge_model = depth_edge_model.to(device)
						depth_edge_model.eval()

						depth_feat_model = Inpaint_Depth_Net()
						depth_feat_weight = torch.load(config['depth_feat_model_ckpt'], map_location=torch.device(device))
						depth_feat_model.load_state_dict(depth_feat_weight, strict=True)
						depth_feat_model = depth_feat_model.to(device)
						depth_feat_model.eval()
						depth_feat_model = depth_feat_model.to(device)
						rgb_model = Inpaint_Color_Net()
						rgb_feat_weight = torch.load(config['rgb_feat_model_ckpt'], map_location=torch.device(device))
						rgb_model.load_state_dict(rgb_feat_weight)
						rgb_model.eval()
						rgb_model = rgb_model.to(device)
						graph = None
						rt_info = write_ply(image, depth, sample['int_mtx'], mesh_fi, config, rgb_model, depth_edge_model, depth_edge_model, depth_feat_model)
						if rt_info is False:
							continue
						rgb_model = None
						color_feat_model = None
						depth_edge_model = None
						depth_feat_model = None
						torch.cuda.empty_cache()
					if config['save_ply'] is True or config['load_ply'] is True:
						verts, colors, faces, Height, Width, hFov, vFov = read_ply(mesh_fi)
					else:
						verts, colors, faces, Height, Width, hFov, vFov = rt_info

					videos_poses, video_basename = copy.deepcopy(sample['tgts_poses']), sample['tgt_name']
					top = (config.get('original_h') // 2 - sample['int_mtx'][1, 2] * config['output_h'])
					left = (config.get('original_w') // 2 - sample['int_mtx'][0, 2] * config['output_w'])
					down, right = top + config['output_h'], left + config['output_w']
					border = [int(xx) for xx in [top, down, left, right]]
					normal_canvas, all_canvas = output_3d_photo(verts.copy(),
					                                            colors.copy(),
					                                            faces.copy(),
					                                            copy.deepcopy(Height),
					                                            copy.deepcopy(Width),
					                                            copy.deepcopy(hFov),
					                                            copy.deepcopy(vFov),
					                                            copy.deepcopy(sample['tgt_pose']),
					                                            sample['video_postfix'],
					                                            copy.deepcopy(sample['ref_pose']),
					                                            copy.deepcopy(config['video_folder']),
					                                            image.copy(),
					                                            copy.deepcopy(sample['int_mtx']),
					                                            config,
					                                            image,
					                                            videos_poses,
					                                            video_basename,
					                                            config.get('original_h'),
					                                            config.get('original_w'),
					                                            border=border,
					                                            depth=depth,
					                                            normal_canvas=normal_canvas,
					                                            all_canvas=all_canvas,
					                                            mean_loc_depth=mean_loc_depth,
					                                            save_file_name=file_name)

					tracker.send("event", "photo", "dummy")

				except Exception as e:
					print("Error :", e)
