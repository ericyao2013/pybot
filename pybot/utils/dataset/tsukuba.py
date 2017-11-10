# Author: Sudeep Pillai <spillai@csail.mit.edu>
# License: MIT

import os 
import cv2
import numpy as np

from pybot.utils.itertools_recipes import izip, repeat
from pybot.utils.dataset_readers import FileReader, DatasetReader, ImageDatasetReader, StereoDatasetReader
from pybot.utils.db_utils import AttrDict

from pybot.vision.image_utils import im_resize
from pybot.vision.camera_utils import StereoCamera
from pybot.geometry.rigid_transform import Quaternion, RigidTransform
from pybot.externals.lcm import draw_utils

def tsukuba_load_poses(fn): 
    """ 
    Retrieve poses
    X Y Z R P Y - > X -Y -Z R -P -Y
    
    np.deg2rad(p[3]),-np.deg2rad(p[4]),-np.deg2rad(p[5]),
        p[0]*.01,-p[1]*.01,-p[2]*.01, axes='sxyz') for p in P ]

    """ 
    P = np.loadtxt(os.path.expanduser(fn), dtype=np.float64, delimiter=',')
    return [ RigidTransform.from_rpyxyz(np.pi, 0, 0, 0, 0, 0) * \
             RigidTransform.from_rpyxyz(
                 np.deg2rad(p[3]),np.deg2rad(p[4]),np.deg2rad(p[5]),
                 p[0]*.01,p[1]*.01,p[2]*.01, axes='sxyz') * \
             RigidTransform.from_rpyxyz(np.pi, 0, 0, 0, 0, 0) for p in P ]
    
    # return [ RigidTransform.from_rpyxyz(
    #     np.deg2rad(p[3]),-np.deg2rad(p[4]),-np.deg2rad(p[5]),
    #     p[0]*.01,-p[1]*.01,-p[2]*.01, axes='sxyz') for p in P ]

class TsukubaStereo2012Reader(object): 
    """
    TsukubaStereo2012Reader: StereoDatasetReader + Calib

    The resolution of the images is 640x480 pixels, the baseline of the stereo 
    camera is 10cm and the focal length of the camera is 615 pixels.

    https://github.com/pablospe/tsukuba_db/blob/master/README.Tsukuba

    """
    calib = StereoCamera.from_calib_params(615, 615, 319.5, 239.5, 
                                           baseline=0.10, shape=np.int32([480, 640]))



    def __init__(self, directory='NewTsukubaStereoDataset/', 
                 left_template='illumination/daylight/left/tsukuba_daylight_L_%05i.png', 
                 right_template='illumination/daylight/right/tsukuba_daylight_R_%05i.png', 
                 start_idx=1, max_files=50000, scale=1.0, grayscale=False): 

        # Minor dataset related check
        if start_idx < 1: 
            raise RuntimeError('TsukubaStereo2012Reader has to start at index 1')

        # Set args
        self.scale = scale

        # Get calib
        self.calib = TsukubaStereo2012Reader.calib.scaled(scale)

        # Read poses
        try: 
            pose_fn = os.path.join(os.path.expanduser(directory), 'groundtruth/camera_track.txt')
            self.poses = FileReader(pose_fn, start_idx=start_idx, process_cb=tsukuba_load_poses)                    
        except Exception as e: 
            self.poses = repeat(None)
            raise RuntimeError('Failed to load poses properly, cannot proceed {:}'.format(e))

        # Read stereo images
        self.stereo = StereoDatasetReader(directory=directory,
                                          left_template=left_template, 
                                          right_template=right_template, 
                                          start_idx=start_idx, max_files=max_files, scale=scale, grayscale=grayscale)
        print 'Initialized stereo dataset reader with %f scale' % scale
        gt_fn = os.path.join(os.path.expanduser(directory),
                             'groundtruth/disparity_maps/left/tsukuba_disparity_L_%05i.png')
        self.gt = DatasetReader(process_cb=TsukubaStereo2012Reader.dispread_process_cb(scale=scale), template=gt_fn, 
                                start_idx=start_idx, max_files=max_files)
        
    @staticmethod
    def dispread_process_cb(scale=1.0):
        """Scale disparity values for depth images"""
        return lambda fn: im_resize(cv2.imread(fn, cv2.IMREAD_UNCHANGED), scale=scale) * scale


    def iterframes(self, *args, **kwargs): 
        for (left, right), pose in izip(self.iter_stereo_frames(*args, **kwargs), 
                                               self.poses.iteritems(*args, **kwargs)):
            yield AttrDict(left=left, right=right, pose=pose)

    def iter_gt_frames(self, *args, **kwargs): 
        for (left, right), pose, depth in izip(self.iter_stereo_frames(*args, **kwargs), 
                                               self.poses.iteritems(*args, **kwargs), 
                                               self.gt.iteritems(*args, **kwargs)): 
            yield AttrDict(left=left, right=right, pose=pose, depth=depth)

    def iter_stereo_frames(self, *args, **kwargs): 
        return self.stereo.iteritems(*args, **kwargs)

    @property
    def stereo_frames(self): 
        return self.iter_stereo_frames()

    def viz_gt_poses(self): 
        draw_utils.publish_pose_list('POSES', self.poses.items, frame_id='camera')

def tsukuba_stereo_dataset(directory='~/HD1/data/NewTsukubaStereoDataset/', scale=1.0, grayscale=False, start_idx=1): 
    return TsukubaStereo2012Reader(directory=directory, scale=scale, grayscale=grayscale, start_idx=start_idx)

if __name__ == "__main__": 
    from pybot.vision.imshow_utils import imshow_cv
    from pybot.vision.image_utils import to_gray

    dataset = tsukuba_stereo_dataset()
    for f in dataset.iterframes():
        lim, rim = to_gray(f.left), to_gray(f.right)
        out = np.dstack([np.zeros_like(lim), lim, rim])
        imshow_cv('left/right', out)
        imshow_cv('disp', f.depth)
