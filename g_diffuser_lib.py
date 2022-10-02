"""
MIT License

Copyright (c) 2022 Christopher Friesen
https://github.com/parlance-zz

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


g_diffuser_lib.py - core diffuser / grpc client operations and lib utilities

"""

from importlib.machinery import PathFinder
import ntpath # these lines are inexplicably required for python to use long file paths on Windows -_-
ntpath.realpath = ntpath.abspath

from g_diffuser_config import DEFAULT_PATHS, GRPC_SERVER_SETTINGS
from g_diffuser_defaults import DEFAULT_SAMPLE_SETTINGS

import os
import datetime
import argparse
import uuid
import pathlib
import json
import re
import subprocess
import psutil
import glob
import hupper

import numpy as np
import PIL            # ...
from PIL import Image # ...
import cv2

#from extensions import grpc_server, grpc_client  # ideally we'd want to keep the server inside the first g-diffuser-lib frontend that is running on this machine
from extensions import grpc_client
from extensions import g_diffuser_utilities as gdl_utils

import torch
from torch import autocast

def _p_kill(proc_pid):  # kill all child processes, recursively as well. its the only way to be sure
    print("Killing process id " + str(proc_pid))
    try:
        process = psutil.Process(proc_pid)
        for proc in process.children(recursive=True): proc.kill()
        process.kill()
    except Exception as e: print("Error killing process id " + str(proc_pid) + " - " + str(e))
    return
    
def run_string(run_string, cwd, show_output=False, log_path=""):  # run shell command asynchronously, return subprocess
    print(run_string + " (cwd="+str(cwd)+")")
    if log_path != "": process = subprocess.Popen(run_string, shell=False, cwd=cwd, stdout=open(log_path, "w", 1))
    else: process = subprocess.Popen(run_string, shell=False, cwd=cwd)
    assert(process)
    return process
    
def valid_resolution(width, height, init_image=None):  # clip dimensions at max resolution, while keeping the correct resolution granularity,
                                                       # while roughly preserving aspect ratio. if width or height are None they are taken from the init_image
    global DEFAULT_SAMPLE_SETTINGS
    
    if not init_image:
        if not width: width = DEFAULT_SAMPLE_SETTINGS.resolution[0]
        if not height: height = DEFAULT_SAMPLE_SETTINGS.resolution[1]
    else:
        if not width: width = init_image.size[0]
        if not height: height = init_image.size[1]
        
    aspect_ratio = width / height 
    if width > DEFAULT_SAMPLE_SETTINGS.max_resolution[0]:
        width = DEFAULT_SAMPLE_SETTINGS.max_resolution[0]
        height = int(width / aspect_ratio + .5)
    if height > DEFAULT_SAMPLE_SETTINGS.max_resolution[1]:
        height = DEFAULT_SAMPLE_SETTINGS.max_resolution[1]
        width = int(height * aspect_ratio + .5)
        
    width = int(width / float(DEFAULT_SAMPLE_SETTINGS.resolution_granularity) + 0.5) * DEFAULT_SAMPLE_SETTINGS.resolution_granularity
    height = int(height / float(DEFAULT_SAMPLE_SETTINGS.resolution_granularity) + 0.5) * DEFAULT_SAMPLE_SETTINGS.resolution_granularity
    width = np.maximum(width, DEFAULT_SAMPLE_SETTINGS.resolution_granularity)
    height = np.maximum(height, DEFAULT_SAMPLE_SETTINGS.resolution_granularity)

    return int(width), int(height)
    
def get_random_string(digits=8):
    uuid_str = str(uuid.uuid4())
    return uuid_str[0:digits] # shorten uuid, don't need that many digits

def print_namespace(namespace, debug=False, verbosity_level=0, indent=4):
    namespace_dict = vars(strip_args(namespace, level=verbosity_level))
    if debug:
        for arg in namespace_dict: print(arg+"='"+str(namespace_dict[arg]) + "' "+str(type(namespace_dict[arg])))
    else:
        print(json.dumps(namespace_dict, indent=indent))
    return

def get_default_output_name(args, truncate_length=70):
    sanitized_name = re.sub(r'[\\/*?:"<>|]',"", args.prompt).replace(".","").replace("'","").replace('"',"").replace("\t"," ").replace(" ","_").strip()
    if (truncate_length > len(sanitized_name)) or (truncate_length==0): truncate_length = len(sanitized_name)
    if truncate_length < len(sanitized_name):  sanitized_name = sanitized_name[0:truncate_length]
    return sanitized_name

def get_noclobber_checked_path(base_path, file_path):
    full_path = base_path+"/"+file_path
    if os.path.exists(full_path):
        file_path_noext, file_path_ext = os.path.splitext(file_path)
        existing_count = len(glob.glob(base_path+"/"+file_path_noext+"*"+file_path_ext)); assert(existing_count > 0)
        return file_path_noext+"_x"+str(existing_count)+file_path_ext
    else:
        return file_path
        
def save_json(_dict, file_path):
    assert(file_path)
    (pathlib.Path(file_path).parents[0]).mkdir(exist_ok=True)
    with open(file_path, "w") as file:
        json.dump(_dict, file, indent=4)
        file.close()
    return
    
def load_json(file_path):
    assert(file_path)
    (pathlib.Path(file_path).parents[0]).mkdir(exist_ok=True)
    
    with open(file_path, "r") as file:
        data = json.load(file)
        file.close()
    return data
    
def strip_args(args, level=0): # remove args we wouldn't want to print or serialize, higher levels strip additional irrelevant fields
    args_stripped = argparse.Namespace(**(vars(args).copy()))
    if "grpc_server_process" in args_stripped: del args_stripped.grpc_server_process
    
    if level >=1: # keep just the basics for most printing
        if "command" in args_stripped: del args_stripped.command
        if "debug" in args_stripped: del args_stripped.debug
        if "interactive" in args_stripped: del args_stripped.interactive
        if "load_args" in args_stripped: del args_stripped.load_args
        
        if "init_time" in args_stripped: del args_stripped.init_time
        if "start_time" in args_stripped: del args_stripped.start_time
        if "end_time" in args_stripped: del args_stripped.end_time
        if "elapsed_time" in args_stripped: del args_stripped.elapsed_time

        if "output_path" in args_stripped: del args_stripped.output_path
        if "final_output_path" in args_stripped: del args_stripped.final_output_path
        if "output_name" in args_stripped: del args_stripped.output_name
        if "final_output_name" in args_stripped: del args_stripped.final_output_name
        if "output_file" in args_stripped: del args_stripped.output_file
        if "output_file_type" in args_stripped: del args_stripped.output_file_type
        if "args_file" in args_stripped: del args_stripped.args_file
        if "no_json" in args_stripped: del args_stripped.no_json

        if "uuid_str" in args_stripped: del args_stripped.uuid_str
        if "status" in args_stripped: del args_stripped.status
        if "err_txt" in args_stripped: del args_stripped.err_txt

        if "init_img" in args_stripped:
            if args_stripped.init_img == "": # if there was no input image these fields are not relevant
                del args_stripped.init_img
                if "noise_q" in args_stripped: del args_stripped.noise_q
                if "strength" in args_stripped: del args_stripped.strength

    return args_stripped
    
def get_grid_layout(num_samples):
    def factorize(num):
        return [n for n in range(1, num + 1) if num % n == 0]
    factors = factorize(num_samples)
    median_factor = factors[len(factors)//2]
    rows = median_factor
    columns = num_samples // rows
    return (columns, rows)
    
def get_image_grid(imgs, layout, mode="columns"): # make an image grid out of a set of images
    assert len(imgs) == layout[0]*layout[1]
    width, height = (imgs[0].shape[0], imgs[0].shape[1])

    np_grid = np.zeros((layout[0]*width, layout[1]*height, 3), dtype="uint8")
    for i, img in enumerate(imgs):
        if mode != "rows":
            paste_x = i // layout[1] * width
            paste_y = i % layout[1] * height
        else:
            paste_x = i % layout[0] * width
            paste_y = i // layout[0] * height
        np_grid[paste_x:paste_x+width, paste_y:paste_y+height, :] = img[:]

    return np_grid

def load_image(args):
    global DEFAULT_PATHS, DEFAULT_SAMPLE_SETTINGS
    assert(DEFAULT_PATHS.inputs)
    final_init_img_path = (pathlib.Path(DEFAULT_PATHS.inputs) / args.init_img).as_posix()
    
    # load and resize input image to multiple of 8x8
    init_image = cv2.imread(final_init_img_path, cv2.IMREAD_UNCHANGED)
    width, height = valid_resolution(args.w, args.h, init_image=init_image)
    if (width, height) != init_image.size:
        if args.debug: print("Resizing input image to (" + str(width) + ", " + str(height) + ")")
        init_image = cv2.resize(init_image, (width, height), interpolation=cv2.INTER_LANCZOS4)
    args.w = width
    args.h = height
    
    num_channels = init_image.shape[2]
    if num_channels == 4: # input image has an alpha channel, setup mask for in/out-painting
        # prep masks, note that you only need to prep masks once if you're doing multiple samples
        mask_image = init_image.split()[-1]
        np_mask_rgb = (np.asarray(mask_image.convert("RGB"))/255.).astype(np.float64)
        mask_image = PIL.Image.fromarray(np.clip(np_mask_rgb*255., 0., 255.).astype(np.uint8), mode="RGB")

    elif num_channels == 3: # rgb image, setup img2img
        if args.strength == 0.: args.strength = DEFAULT_SAMPLE_SETTINGS.strength
        blend_mask = gdl_utils.np_img_grey_to_rgb(np.ones((args.w, args.h)) * np.clip(args.strength**(0.075), 0., 1.)) # todo: find strength mapping or do a better job of seeding
        mask_image = PIL.Image.fromarray(np.clip(blend_mask*255., 0., 255.).astype(np.uint8), mode="RGB")

    else:
        print("Error loading init_image "+final_init_img_path+": unsupported image format in ")
        return None, None

    return init_image, mask_image
        
def get_samples(args, write=True):
    global DEFAULT_PATHS
    global DEFAULT_SAMPLE_SETTINGS, GRPC_SERVER_SETTINGS
    
    assert((args.n > 0) or write) # repeating forever without writing to disk wouldn't make much sense

    if not args.output_name: args.final_output_name = get_default_output_name(args)
    else: args.final_output_name = args.output_name
    if not args.output_path: args.final_output_path = args.final_output_name
    else: args.final_output_path = args.output_path
        
    if not args.seed: # no seed provided
        if not ("auto_seed" in args): # no existing auto-seed
            args.auto_seed = int(np.random.randint(DEFAULT_SAMPLE_SETTINGS.auto_seed_range[0], DEFAULT_SAMPLE_SETTINGS.auto_seed_range[1])) # new random auto-seed
    else:
        if ("auto_seed" in args): del args.auto_seed # if a seed is provided just strip out the auto_seed entirely

    if args.init_img != "": # load input image if we have one
        init_image, mask_image = load_image(args)
    else:
        init_image, mask_image = (None, None)
        if not args.w: args.w = DEFAULT_SAMPLE_SETTINGS.resolution[0] # if we don't have an input image, it's size can't be used as the default resolution
        if not args.h: args.h = DEFAULT_SAMPLE_SETTINGS.resolution[1]

    stability_api = grpc_client.StabilityInference(GRPC_SERVER_SETTINGS.host, GRPC_SERVER_SETTINGS.key, engine=args.model_name, verbose=False)

    samples = []
    while True: # watch out! a wild shrew!
        try:
            request_dict = build_grpc_request_dict(args)
            answers = stability_api.generate(args.prompt, **request_dict)
            grpc_output_prefix = DEFAULT_PATHS.temp+"/s"
            grpc_samples = grpc_client.process_artifacts_from_answers(grpc_output_prefix, answers, write=False, verbose=False)

            start_time = datetime.datetime.now()
            args.start_time = str(start_time)
            for path, artifact in grpc_samples:
                end_time = datetime.datetime.now()
                args.end_time = str(end_time)
                args.elapsed_time = str(end_time-start_time)
                args.status = 2 # completed successfully
                args.err_txt = ""

                image = np.fromstring(artifact.binary, dtype="uint8")
                image = cv2.imdecode(image, cv2.IMREAD_UNCHANGED)
                samples.append(image)

                if write: # save sample to disk if write=True
                    args.uuid_str = get_random_string(digits=16) # new uuid for new sample
                    save_sample(image, args)

                if args.seed: args.seed += 1 # increment seed or random seed if none was given as we go through the batch
                else: args.auto_seed += 1
                if (len(samples) < args.n) or (args.n <= 0): # reset start time if we still have samples left to generate
                    start_time = datetime.datetime.now()
                    args.start_time = str(start_time)

            if args.n > 0: break # if we had a set number of samples then we are done

        except Exception as e:
            if args.debug: raise
            args.status = -1 # error status
            args.err_txt = str(e)
            return samples

    if write and len(samples) > 1: # if batch size > 1 and write to disk is enabled, save composite "grid image"
        args.uuid_str = get_random_string(digits=16) # new uuid for new "sample"
        save_samples_grid(samples, args)

    return samples

def save_sample(sample, args):
    global DEFAULT_PATHS
    assert(DEFAULT_PATHS.outputs)

    if args.seed: seed = args.seed
    else: seed = args.auto_seed

    pathlib.Path(DEFAULT_PATHS.outputs+"/"+args.final_output_path).mkdir(exist_ok=True)
    args.output_file = args.final_output_path+"/"+args.final_output_name+"_s"+str(seed)+".png"
    args.output_file = get_noclobber_checked_path(DEFAULT_PATHS.outputs, args.output_file) # add suffix if filename already exists
    args.output_file_type = "img" # the future is coming, hold on to your butts
    cv2.imwrite(DEFAULT_PATHS.outputs+"/"+args.output_file, sample)
    print("Saved " + str(DEFAULT_PATHS.outputs+"/"+args.output_file))

    if not args.no_json:
        args.args_file = args.final_output_path+"/json/"+args.final_output_name+"_s"+str(seed)+".json"
        args.args_file = get_noclobber_checked_path(DEFAULT_PATHS.outputs, args.args_file) # add suffix if filename already exists
        save_json(vars(strip_args(args)), DEFAULT_PATHS.outputs+"/"+args.args_file)

    return
    
def save_samples_grid(samples, args):
    assert(len(samples)> 1)
    grid_layout = get_grid_layout(len(samples))
    grid_image = get_image_grid(samples, grid_layout)
    args.output_file = args.final_output_path+"/grid_"+args.final_output_name+".jpg"
    args.output_file = get_noclobber_checked_path(DEFAULT_PATHS.outputs, args.output_file)
    args.output_file_type = "grid_img"
    cv2.imwrite(DEFAULT_PATHS.outputs+"/"+args.output_file, grid_image)
    print("Saved grid " + str(DEFAULT_PATHS.outputs+"/"+args.output_file))
    return

def start_grpc_server(args):
    global DEFAULT_PATHS, GRPC_SERVER_SETTINGS
    if args.debug: load_start_time = datetime.datetime.now()
    
    if DEFAULT_PATHS.grpc_log != DEFAULT_PATHS.root:
        log_path = DEFAULT_PATHS.grpc_log
    else:
        log_path = ""
    
    """
    from extensions import grpc_server
    reloader = hupper.start_reloader('grpc_server.main', reload_interval=10)
    with open(os.path.normpath(args.enginecfg), 'r') as cfg:
        engines = yaml.load(cfg, Loader=Loader)
        manager = EngineManager(engines, weight_root=args.weight_root, enable_mps=args.enable_mps, vram_optimisation_level=args.vram_optimisation_level, nsfw_behaviour=args.nsfw_behaviour)
        start(manager, "*:5000" if args.listen_to_all else "localhost:5000")
    """

    grpc_server_run_string = "python ./server.py"
    grpc_server_run_string += " --enginecfg "+DEFAULT_PATHS.root+"/g_diffuser_config_models.yaml" + " --weight_root "+DEFAULT_PATHS.models
    grpc_server_run_string += " --vram_optimisation_level " + str(GRPC_SERVER_SETTINGS.memory_optimization_level)
    if GRPC_SERVER_SETTINGS.enable_mps: grpc_server_run_string += " --enable_mps"
    grpc_server_process = run_string(grpc_server_run_string, cwd=DEFAULT_PATHS.extensions+"/"+"stable-diffusion-grpcserver", log_path=log_path)
    
    if args.debug: print("sd_grpc_server start time : " + str(datetime.datetime.now() - load_start_time))
    
    args.grpc_server_process = grpc_server_process
    return grpc_server_process
 
def get_args_parser():
    global DEFAULT_SAMPLE_SETTINGS
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="",
        help="the text to condition sampling on",
    )
    parser.add_argument(
        "--model-name",
        type=str,
        default=DEFAULT_SAMPLE_SETTINGS.model_name,
        help="diffusers model name",
    )
    parser.add_argument(
        "--sampler",
        type=str,
        default="k_euler",
        help="sampler to use (ddim, plms, k_euler, k_euler_ancestral, k_heun, k_dpm_2, k_dpm_2_ancestral, k_lms)"
    )  
    parser.add_argument(
        "--command",
        type=str,
        default="sample",
        help="diffusers command to execute",
    )    
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="random starting seed for sampling (0 for random)",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=DEFAULT_SAMPLE_SETTINGS.steps,
        help="number of sampling steps (number of times to refine image)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=DEFAULT_SAMPLE_SETTINGS.scale,
        help="classifier-free guidance scale (~amount of change per step)",
    )
    parser.add_argument(
        "--noise_q",
        type=float,
        default=DEFAULT_SAMPLE_SETTINGS.noise_q,
        help="falloff of shaped noise distribution for in/out-painting ( > 0), 1 is matched, lower values mean smaller features and higher means larger features",
    )
    parser.add_argument(
        "--strength",
        type=float,
        default=0.,
        help="overall amount to change the input image (default value defined in DEFAULT_SAMPLE_SETTINGS)",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=DEFAULT_SAMPLE_SETTINGS.n,
        help="number of samples to generate",
    )
    parser.add_argument(
        "--w",
        type=int,
        default=None,
        help="set output width or override width of input image",
    )
    parser.add_argument(
        "--h",
        type=int,
        default=None,
        help="set output height or override height of input image",
    )
    parser.add_argument(
        "--init-img",
        type=str,
        default="",
        help="path to the input image",
    )
    parser.add_argument(
        "--output_path",
        type=str,
        help="path to store output samples (relative to root outputs path, by default this uses the prompt)",
        default="",
    )
    parser.add_argument(
        "--output_name",
        type=str,
        help="normally output files are named for the their prompt and seed, override to use a static name instead",
        default="",
    )
    parser.add_argument(
        "--interactive",
        action='store_true',
        default=False,
        help="enters an interactive command line mode to generate multiple samples",
    )
    parser.add_argument(
        "--load-args",
        type=str,
        default="",
        help="if set, preload and use a saved set of arguments from a json file in your inputs path",
    )
    parser.add_argument(
        "--no-json",
        action='store_true',
        default=False,
        help="disable saving arg files for each sample output in output path/json",
    )
    parser.add_argument(
        "--debug",
        action='store_true',
        default=False,
        help="enable verbose CLI output and debug image dumps",
    )
    
    return parser
    
def get_default_args():
    return get_args_parser().parse_args()
    
def build_grpc_request_dict(args):
    global DEFAULT_SAMPLE_SETTINGS
    
    # use auto-seed if none provided
    if args.seed: seed = args.seed
    else:
        args.auto_seed
        seed = args.auto_seed
    
    # if repeating just use the default batch size
    if args.n <= 0: n = int(1e10) #DEFAULT_SAMPLE_SETTINGS.batch_size
    else: n = args.n

    return {
        "height": args.h,
        "width": args.w,
        "start_schedule": None, #args.start_schedule,
        "end_schedule": None,   #args.end_schedule,
        "cfg_scale": args.scale,
        "eta": 0.,              #args.eta,
        "sampler": grpc_client.get_sampler_from_str(args.sampler),
        "steps": args.steps,
        "seed": seed,
        "samples": n,
        "init_image": None, #args.init_image,
        "mask_image": None, #args.mask_image,
        #"negative_prompt": args.negative_prompt
    }    