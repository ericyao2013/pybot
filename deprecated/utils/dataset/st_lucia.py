import os.path
import numpy as np
from scipy.io import loadmat

from pybot.utils.itertools_recipes import izip, islice
from pybot.geometry.rigid_transform import RigidTransform
from pybot.utils.io_utils import VideoCapture
from pybot.utils.db_utils import AttrDict
from pybot.mapping.nav_utils import metric_from_gps, bearing_from_metric_gps

class StLuciaReader(object):
    def __init__(self, directory, start_at_origin=True): 

        # Read dataset
        self.video_path_ = os.path.join(
            os.path.expanduser(directory), 'webcam_video.avi')

        # Metric coords from GPS
        gps = loadmat(os.path.join(
            os.path.expanduser(directory), 'fGPS.mat'))['fGPS']
        mgps = metric_from_gps(gps)
        assert(np.isfinite(mgps).all())
        mgps -= mgps.mean(axis=0)

        # Determine bearing from Sequential GPS
        theta = bearing_from_metric_gps(mgps)
        assert(np.isfinite(theta).all())
        self.poses_ = [ RigidTransform.from_rpyxyz(0,0,th,gps[1],gps[0],0)
                        for gps,th in zip(mgps, theta) ]

        if start_at_origin: 
            self.poses_ = [ self.poses_[0].inverse() * p for p in self.poses_]
        
    def iterframes(self):
        cap = VideoCapture(self.video_path_)
        for (img,pose) in izip(cap.iteritems(), self.poses_):
            yield AttrDict(img=img, pose=pose)

    @property
    def poses(self):
        return self.poses_

    @property
    def length(self):
        return self.len(self.poses_)
        
