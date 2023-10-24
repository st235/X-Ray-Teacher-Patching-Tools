import argparse
import datetime
import os
import numpy as np
import open3d as o3d
import multiprocessing
from multiprocessing import Manager, Pool
from functools import partial
import sys
import time

from tqdm import tqdm

sys.path.append(os.path.join(os.path.dirname(__file__), './gedi'))
from src.accumulation.accumulation_strategy import AccumulationStrategy
from src.accumulation.default_accumulator_strategy import DefaultAccumulatorStrategy
from src.accumulation.gedi_accumulator_strategy import GediAccumulatorStrategy
from src.accumulation.point_cloud_accumulator import PointCloudAccumulator

from src.datasets.dataset import Dataset
from src.datasets.nuscenes.nuscenes_dataset import NuscenesDataset
from src.datasets.once.once_dataset import OnceDataset
from src.datasets.waymo.waymo_dataset import WaymoDataset
from src.utils.dataset_helper import group_instances_across_frames
from src.utils.logging_utils import create_logger
from src.utils.o3d_helper import convert_to_o3d_pointcloud

logging = create_logger()


def __patch_scene(scene_id: str,
                  accumulation_strategy: AccumulationStrategy,
                  dataset: Dataset,
                  export_instances: bool,
                  export_frames: bool,
                  force_overwrite: bool,
                  gedi_counter):
    if isinstance(accumulation_strategy, GediAccumulatorStrategy):
        gpu_id = -1
        while gpu_id == -1:
            for i in range(torch.cuda.device_count()):
                if gedi_counter[i] < 4:
                    gpu_id = i
                    gedi_counter[i] += 1
                    break
            if gpu_id == -1:
                time.sleep(1)  # wait for a GPU to be available

        torch.cuda.set_device(gpu_id)
        logging.info(f"Running GediAccumulatorStrategy on GPU {gpu_id}")

    logging.info(f"[Scene {scene_id}] Starting...")

    grouped_instances = group_instances_across_frames(scene_id=scene_id, dataset=dataset)

    point_cloud_accumulator = PointCloudAccumulator(step=1,
                                                    grouped_instances=grouped_instances,
                                                    dataset=dataset)

    instance_accumulated_clouds_lookup = dict()

    current_instance_index = 0
    overall_instances_to_process_count = len(grouped_instances)

    output_folder = './temp/ply_instances_geo/'
    output_folder_frame = './temp/ply_frames_geo/'

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    if not os.path.exists(output_folder_frame):
        os.makedirs(output_folder_frame)

    for instance in grouped_instances.keys():
        logging.info(f"[Scene {scene_id}] Merging {instance}")

        assert instance not in instance_accumulated_clouds_lookup

        accumulated_point_cloud = point_cloud_accumulator.merge(scene_id=scene_id,
                                                                instance_id=instance,
                                                                accumulation_strategy=accumulation_strategy)

        if export_instances:
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            filename = os.path.join(output_folder, f"{instance}_{timestamp}.ply")
            accumulated_point_cloud_o3d = convert_to_o3d_pointcloud(
                accumulated_point_cloud.T)  # obj instance accumulated
            o3d.io.write_point_cloud(filename, accumulated_point_cloud_o3d)

        instance_accumulated_clouds_lookup[instance] = accumulated_point_cloud

        current_instance_index += 1
        logging.info(
            f"[Scene {scene_id}] Merged {int((current_instance_index / overall_instances_to_process_count) * 100)}% "
            f"of instances.")

    frames_to_instances_lookup: dict = dict()
    for instance, frames in grouped_instances.items():
        for frame_id in frames:
            if not force_overwrite and not dataset.can_serialise_frame_point_cloud(scene_id=scene_id,
                                                                                   frame_id=frame_id):
                logging.warning(f"[Scene {scene_id}] Skipping frame {frame_id}...")
                continue

            if frame_id not in frames_to_instances_lookup:
                frames_to_instances_lookup[frame_id] = set()
            frames_to_instances_lookup[frame_id].add(instance)

    current_frame_index = 0
    overall_frames_to_patch_count = len(frames_to_instances_lookup)
    logging.info(f"[Scene {scene_id}] Found {overall_frames_to_patch_count} frames to patch.")

    for frame_id, instances in frames_to_instances_lookup.items():
        logging.info(f"[Scene {scene_id}] Patching frame {frame_id}...")

        patcher = dataset.load_frame_patcher(scene_id=scene_id,
                                             frame_id=frame_id)

        for instance in instances:
            # Make sure you copy instance_accumulated_clouds_lookup[instance]
            # to do not carry the rotation and translation in between frames.
            patcher.patch_instance(instance_id=instance,
                                   point_cloud=np.copy(instance_accumulated_clouds_lookup[instance]))

        saved_path = dataset.serialise_frame_point_clouds(scene_id=scene_id,
                                                          frame_id=frame_id,
                                                          frame_point_cloud=patcher.frame)

        if export_frames:
            filename = os.path.join(output_folder_frame, f"{current_frame_index}.ply")
            frame_o3d = convert_to_o3d_pointcloud(patcher.frame.T)  # patched frame
            o3d.io.write_point_cloud(filename, frame_o3d)

        current_frame_index += 1

        if saved_path is not None:
            logging.info(f"[Scene {scene_id}] {int((current_frame_index / overall_frames_to_patch_count) * 100)}%, "
                         f"saved to {saved_path}")
        else:
            logging.error(f"[Scene {scene_id}] There was an error saving the point cloud for frame {frame_id}")

    logging.info(f"[Scene {scene_id}] Wrapping up.")

    # If the strategy is GediAccumulatorStrategy, decrease the counter
    if isinstance(accumulation_strategy, GediAccumulatorStrategy):
        gedi_counter[gpu_id] -= 1

    # Return OK status when finished processing.
    return True


def __process_dataset(dataset: Dataset,
                      accumulation_strategy: AccumulationStrategy,
                      export_instances: bool,
                      export_frames: bool,
                      num_workers: int,
                      force_overwrite: bool,
                      gedi_counter):
    assert num_workers > 0, "num_workers should be positive"

    print(f"Processing dataset from: {dataset.dataroot}")
    logging.info(f"Processing dataset from: {dataset.dataroot}")

    scenes = dataset.scenes
    scenes_count = len(scenes)

    patch_scene = partial(
        __patch_scene,
        accumulation_strategy=accumulation_strategy,
        dataset=dataset,
        export_instances=export_instances,
        export_frames=export_frames,
        force_overwrite=force_overwrite,
        gedi_counter=gedi_counter
    )

    with Pool(num_workers) as p:
        list(tqdm(p.imap_unordered(patch_scene, scenes), total=scenes_count))


accumulator_strategies = {
    'default': DefaultAccumulatorStrategy(),
    'gedi': GediAccumulatorStrategy(),
}


def parse_arguments():
    parser = argparse.ArgumentParser(description='patch scene arguments')
    parser.add_argument('--dataset', type=str, choices=['nuscenes', 'once', 'waymo'], default='nuscenes',
                        help='Dataset.')
    parser.add_argument('--version', type=str, default='v1.0-mini', help='NuScenes version.')
    parser.add_argument("--split", type=str, choices=['train', 'test', 'val', 'raw_small', 'raw_medium', 'raw_large'],
                        default="train", help="Once dataset split type.")
    parser.add_argument('--dataroot', type=str, default='./temp/nuscenes', help='Data root location.')
    parser.add_argument('--strategy', type=str, default='default', help='Accumulation strategy.')
    parser.add_argument('--instances', action='store_true', help='Export instances.')
    parser.add_argument('--frames', action='store_true', help='Export frames.')
    parser.add_argument('--enable_logging', action='store_true', help='Save additional logs to file.')
    parser.add_argument('--num_workers', type=int, default=multiprocessing.cpu_count(),
                        help='Count of parallel workers.')
    parser.add_argument('--force_overwrite', action='store_true', help='Overwrite saved files.')
    return parser.parse_args()


def main():
    multiprocessing.set_start_method('spawn', force=True)
    args = parse_arguments()

    logging.disabled = not args.enable_logging

    dataset_type = args.dataset

    if dataset_type == 'nuscenes':
        dataset = NuscenesDataset(version=args.version, dataroot=args.dataroot)
    elif dataset_type == 'once':
        dataset = OnceDataset(split=args.split, dataset_root=args.dataroot)
    elif dataset_type == 'waymo':
        dataset = WaymoDataset(dataset_root=args.dataroot)
    else:
        raise Exception(f"Unknown dataset {dataset_type}")

    accumulator_strategy = accumulator_strategies[args.strategy]

    with Manager() as manager:
        gedi_counter = manager.dict()
        for i in range(torch.cuda.device_count()):
            gedi_counter[i] = 0

        __process_dataset(dataset=dataset,
                          accumulation_strategy=accumulator_strategy,
                          export_instances=args.instances,
                          export_frames=args.frames,
                          num_workers=args.num_workers,
                          force_overwrite=args.force_overwrite,
                          gedi_counter=gedi_counter)


if __name__ == '__main__':
    main()
