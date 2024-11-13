#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os

import torch
from random import randint
from utils.loss_utils import l1_loss, ssim
from gaussian_renderer import render, network_gui
from diff_gaussian_rasterization import SurfaceAlign
import sys
from scene import Scene, GaussianModel
from utils.general_utils import safe_state
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False

try:
    import wandb
    WANDB_FOUND = True
except ImportError:
    WANDB_FOUND = False

# from lpipsPyTorch import lpips


def training(dataset, opt, pipe, testing_iterations, saving_iterations, checkpoint_iterations, checkpoint, debug_from, sparse_num=1, num_max=None):
    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree)
    scene = Scene(dataset, gaussians, sparse_num=sparse_num)
    gaussians.training_setup(opt)
    if checkpoint:
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, opt)

    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)


    KNN_index = None
    viewpoint_stack = None
    ema_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, opt.iterations), desc="Training progress")
    first_iter += 1

    cum_deleted = 0
    cum_created = 0

    for iteration in range(first_iter, opt.iterations + 1):        

        iter_start.record()

        gaussians.update_learning_rate(iteration)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()

        # Pick a random Camera
        if not viewpoint_stack:
            viewpoint_stack = scene.getTrainCameras().copy()
        viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))
        # viewpoint_cam = viewpoint_stack[0]

        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True

        bg = torch.rand((3), device="cuda") if opt.random_background else background

        render_pkg = render(viewpoint_cam, gaussians, pipe, bg)
        image, image_depth, depth_loss, viewspace_point_tensor, visibility_filter, radii\
            = (render_pkg["render"],
               render_pkg["render_depth"],
               render_pkg["rendered_depth_loss"],
               render_pkg["viewspace_points"],
               render_pkg["visibility_filter"],
               render_pkg["radii"])

        # Loss
        gt_image = viewpoint_cam.original_image.cuda()
        # gt_image_depth = viewpoint_cam.original_image_depth.cuda()
        Ll1 = l1_loss(image, gt_image)
        loss = torch.zeros_like(Ll1)
        loss += (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim(image, gt_image))


        # Geo Loss
        if KNN_index != None:
            pair_d_loss = torch.tensor(0).float().cuda()
            pair_normal_loss = torch.tensor(0).float().cuda()

            # # Pytorch Implementation


            # CUDA Implementation
            if visibility_filter.sum() > 0:
                mask = torch.logical_and(visibility_filter, gaussians.get_type.squeeze() == 1)
                # print("gaussians.get_xyz", gaussians.get_xyz.dtype)
                # print("gaussians.get_rotation", gaussians.get_rotation.dtype)
                # print("gaussians.get_xyz_id.contiguous", gaussians.get_xyz_id.contiguous().dtype)
                # print("KNN_index[mask]", KNN_index[mask].dtype)
                pair_d_loss, pair_normal_loss = SurfaceAlign()(gaussians.get_xyz,
                                                               gaussians.get_xyz_id.contiguous(),
                                                               gaussians.get_rotation,
                                                               KNN_index[mask])
                # print(f' pair_d_loss {torch.mean(pair_d_loss).item():6f} pair_normal_loss(1e-6) {torch.mean(pair_normal_loss).item() * 1e6:5f} ')
                loss += 0.05*torch.mean(pair_d_loss) + 0.01*torch.mean(pair_normal_loss)

        loss.backward()

        iter_end.record()

        n_created, n_deleted = 0, 0

        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log
            if iteration % 10 == 0:
                gs_num = gaussians.get_xyz.shape[0]
                progress_bar.set_postfix({"Loss": f"{loss.item():.4f}", "GS num": f'{gs_num}'})
                progress_bar.update(10)
            if iteration == opt.iterations:
                progress_bar.close()

            # Log and save
            training_report(tb_writer, iteration, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), testing_iterations, scene, render, (pipe, background))
            if (iteration in saving_iterations):
                print("\n[ITER {}] Saving Gaussians".format(iteration))
                scene.save(iteration)
            
            if WANDB_FOUND:
                wandb.log({
                    "train/psnr": psnr(image, gt_image).mean().double(),
                    "train/ssim": ssim(image, gt_image).mean().double(),
                    # "train/lpips": lpips(image, gt_image).mean().double(),
                }, step=iteration)

            # Densification
            if iteration < opt.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    size_threshold = 20 if iteration > opt.opacity_reset_interval else None
                    n_created, n_deleted = gaussians.densify_and_prune(opt.densify_grad_threshold, 0.05, scene.cameras_extent, size_threshold, num_max)
                    KNN_index = gaussians.findKNN()

                # if iteration < 5000 and iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                if iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                    gaussians.reset_opacity()
            elif iteration > opt.densify_until_iter:
                if iteration % 3000 == 0:
                    KNN_index = gaussians.findKNN()

            if WANDB_FOUND:
                cum_deleted = cum_deleted + n_deleted
                cum_created = cum_created + n_created
                wandb.log({
                    "cum_deleted": cum_deleted,
                    "cum_created": cum_created,
                }, step=iteration)

            # Optimizer step
            if iteration < opt.iterations:
                gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none = True)

            if (iteration in checkpoint_iterations):
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt" + str(iteration) + ".pth")

def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])
        
    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : Scene, renderFunc, renderArgs):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar('iter_time', elapsed, iteration)
    
    if WANDB_FOUND:
        wandb.log({
            "n_gaussians": scene.gaussians.get_xyz.shape[0],
        }, step=iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        # validation_configs = ({'name': 'test', 'cameras' : sample(scene.getTestCameras(), 5)},
        validation_configs = (
            {'name': 'test_full', 'cameras' : scene.getTestCameras()},
            {'name': 'train_every_5th', 'cameras' : [scene.getTrainCameras()[idx] for idx in range(0, len(scene.getTrainCameras()), 5)]}
        )

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                # l1_d_test = 0.0
                psnr_test = 0.0
                ssim_test = 0.0
                # lpips_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    render = renderFunc(viewpoint, scene.gaussians, *renderArgs)
                    image = torch.clamp(render["render"], 0.0, 1.0)
                    image_d = render["render_depth"]
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    # gt_image_depth = viewpoint.original_image_depth.cuda()
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                    l1_test += l1_loss(image, gt_image).mean().double()
                    # l1_d_test += l1_loss(image_d, gt_image_depth).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                    ssim_test += ssim(image, gt_image)
                    # lpips_test += lpips(image, gt_image, net_type='alex').item()


                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])
                # l1_d_test /= len(config['cameras'])
                ssim_test /= len(config['cameras'])
                # lpips_test /= len(config['cameras'])
                print("\n[ITER {}] Evaluating {}: L1 {:0.4f} PSNR {:0.2f} SSIM {:0.3f}".format(iteration, config['name'], l1_test, psnr_test, ssim_test))
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - ssim', ssim_test, iteration)
                
                if WANDB_FOUND:
                    wandb.log({
                        f"{config['name']}/psnr": psnr_test,
                        f"{config['name']}/ssim": ssim_test,
                        # 'test/lpips': lpipss_test,
                    }, step=iteration)

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
        torch.cuda.empty_cache()

def init_wandb(wandb_key: str, wandb_project: str, wandb_run_name: str, model_path: str, args):
    if WANDB_FOUND:
        import hashlib
        wandb.login(key=wandb_key)
        id = hashlib.md5(wandb_run_name.encode('utf-8')).hexdigest()
        wandb_run = wandb.init(
            project=wandb_project,
            name=wandb_run_name,
            config=args,
            dir=model_path,
            mode="online",
            id=id,
            resume=True
        )
        return wandb_run
    else:
        return None

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--sparse_num', type=int, default=1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[10, 1000, 2000, 3000, 4000, 5000, 6000, 7_000, 10_000, 15_000, 25_000, 30_000])
    # parser.add_argument("--test_iterations", nargs="+", type=int, default=[7_000, 10_000, 15_000,  30_000])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[2000, 7_000, 10_000, 15_000, 25_000, 30_000])
    # parser.add_argument("--save_iterations", nargs="+", type=int, default=[7_000, 10_000, 15_000,  30_000])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[7000, 30_000])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    parser.add_argument("--num_max", type=int, default = None, help="Maximum number of splats in the scene")
    parser.add_argument("--wandb_key", type=str, default="", help="The key used to sign into weights & biases logging")
    parser.add_argument("--wandb_project", type=str, default="")
    parser.add_argument("--wandb_run_name", type=str, default=None)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)

    wand_run = init_wandb(args.wandb_key, args.wandb_project, args.wandb_run_name, args.model_path, args)

    try:
    
        print("Optimizing " + args.model_path)

        # Initialize system state (RNG)
        safe_state(args.quiet)

        # Start GUI server, configure and run training
        network_gui.init(args.ip, args.port)
        torch.autograd.set_detect_anomaly(args.detect_anomaly)
        training(
            lp.extract(args), 
            op.extract(args), 
            pp.extract(args), 
            args.test_iterations, 
            args.save_iterations, 
            args.checkpoint_iterations, 
            args.start_checkpoint, 
            args.debug_from, 
            sparse_num=args.sparse_num,
            num_max=args.num_max)

        # All done
        print("\nTraining complete.")
    
    finally:
        if wand_run is not None:
            wand_run.finish()
