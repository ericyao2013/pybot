#!/usr/bin/env python
import time
import argparse
import numpy as np

from pybot.geometry.rigid_transform import RigidTransform, Pose
from pybot.utils.dataset.kitti import KITTIDatasetReader
from pybot.utils.test_utils import test_dataset
from pybot.utils.timer import SimpleTimer
from pybot.vision.imshow_utils import imshow_cv

import pybot.externals.draw_utils as draw_utils

if __name__ == "__main__": 

    parser = argparse.ArgumentParser(
        description='KITTI test dataset')
    parser.add_argument(
        '--velodyne', dest='velodyne', action='store_true',
        help="Process Velodyne data")
    args = parser.parse_args()


    # KITTI params
    dataset = test_dataset(sequence='00', scale=1.0)

    try: 
        # Publish ground truth poses
        draw_utils.publish_pose_list('ground_truth_poses', dataset.poses, frame_id='camera')

        # Publish line segments
        pts = np.vstack([map(lambda p: p.tvec, dataset.poses)])
        draw_utils.publish_line_segments(
            'ground_truth_trace',
            pts[:-1], pts[1:], frame_id='camera')

    except Exception as e:
        print('Failed to publish poses, {}'.format(e))
        
    # Iterate through the dataset
    p_bc = KITTIDatasetReader.camera2body
    p_bv = KITTIDatasetReader.velodyne2body

    p_bc, p_bv = dataset.p_bc, dataset.p_bv
    p_cv = (p_bc.inverse() * p_bv).inverse()

    timer = SimpleTimer('publish_velodyne')
    
    # Iterate through frames
    for idx, f in enumerate(dataset.iterframes()):
        # imshow_cv('frame', np.vstack([f.left,f.right]))

        # Publish keyframes every 5 frames
        if idx % 5 == 0: 
            draw_utils.publish_cameras(
                'cam_poses', [Pose.from_rigid_transform(idx, f.pose)],
                frame_id='camera', zmax=2,
                reset=False, draw_faces=False, draw_edges=True)

        # Publish pose 
        draw_utils.publish_pose_list(
            'poses', [Pose.from_rigid_transform(idx, f.pose)],
            frame_id='camera', reset=False)

        # Move camera viewpoint 
        draw_utils.publish_pose_t('CAMERA_POSE', f.pose,
                                  frame_id='camera')

        if args.velodyne and idx % 5 == 0:

            # Collect velodyne point clouds (+ve x axis)
            X_v = f.velodyne[::4,:3]
            # carr = f.velodyne[::10,3]
            # carr = np.tile(carr.reshape(-1,1), [1,3])

            carr = draw_utils.height_map(X_v[:,2], hmin=-2, hmax=4)
            
            inds, = np.where(X_v[:,0] >= 0)
            X_v = X_v[inds, :3]
            carr = carr[inds]

            # Convert velo pt. cloud to cam coords, and project
            X_c = p_cv * X_v
            draw_utils.publish_cloud(
                'cloud', X_c, c=carr, frame_id='poses',
                element_id=idx, reset=False)
            