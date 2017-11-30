#!/bin/env python
'''
class for working with calibrated stereo camera pairs.
'''

from argparse import ArgumentParser
from blockmatchers import BadBlockMatcherArgument, StereoBM, StereoSGBM
from calibrate_stereo import StereoCalibration
from calibrate_stereo import find_files
from functools import partial
from point_cloud import PointCloud
from webcams import StereoPair

import cv2
import numpy as np
from pybot.vision.imshow_utils import imshow_cv
from pybot_externals import StereoELAS

class CalibratedPair(StereoPair):
    """
    A stereo pair of calibrated cameras.

    Should be initialized with a context manager to ensure that the camera
    connections are closed properly.
    """
    def __init__(self, devices, calibration, block_matcher):
        """
        Initialize cameras.

        ``devices`` is an iterable of the device numbers. If you want to use the
        ``CalibratedPair`` in offline mode, pass None.
        ``calibration`` is a StereoCalibration object.
        ``block_matcher`` is a BlockMatcher object.
        """
        if devices:
            super(CalibratedPair, self).__init__(devices)
        #: ``StereoCalibration`` object holding the camera pair's calibration
        self.calibration = calibration
        #: ``BlockMatcher`` object for computing disparity and point cloud
        self.block_matcher = block_matcher
    def get_frames(self):
        """Rectify and return current frames from cameras."""
        frames = super(CalibratedPair, self).get_frames()
        return self.calibration.rectify(frames)

    def get_point_cloud(self, pair):
        """Get 3D point cloud from image pair."""
        disparity = self.block_matcher.compute_disparity(pair)
        points = self.block_matcher.compute_3d(disparity,
                                           self.calibration.disp_to_depth_mat)
        colors = cv2.cvtColor(pair[0], cv2.COLOR_BGR2RGB)
        return PointCloud(points, colors)

class BMTuner(object):
    """
    A class for tuning Stereo BM settings.

    Display a normalized disparity picture from two pictures captured with a
    ``CalibratedPair`` and allow the user to manually tune the settings for the
    stereo block matcher.
    """
    #: Window to show results in
    window_name = "BM Tuner"
    def __init__(self, block_matcher, calibration, image_pair):
        """Initialize tuner with a ``CalibratedPair`` and tune given pair."""
        #: Stereo calibration to find Stereo BM settings for
        self.calibration = calibration
        #: (left, right) image pair to find disparity between
        self.pair = image_pair
        #: Block matcher to be tuned
        self.block_matcher = block_matcher
        #: Shortest dimension of image
        self.shortest_dimension = min(self.pair[0].shape[:2])
        #: Settings chosen for ``BlockMatcher``
        self.bm_settings = {}
        for parameter in self.block_matcher.parameter_maxima.keys():
            self.bm_settings[parameter] = []
        cv2.namedWindow(self.window_name)
        self._initialize_trackbars()
        self.tune_pair(image_pair)
    def _initialize_trackbars(self):
        """
        Initialize trackbars by discovering settable parameters in BlockMatcher.
        """
        for parameter in self.block_matcher.parameter_maxima.keys():
            maximum = self.block_matcher.parameter_maxima[parameter]
            if not maximum:
                maximum = self.shortest_dimension
            cv2.createTrackbar(parameter, self.window_name,
                               self.block_matcher.__getattribute__(parameter),
                               maximum,
                               partial(self._set_value, parameter))
    def _set_value(self, parameter, new_value):
        """Try setting new parameter on ``block_matcher`` and update map."""
        try:
            self.block_matcher.__setattr__(parameter, new_value)
        except BadBlockMatcherArgument:
            return
        self.update_disparity_map()
    def _save_bm_state(self):
        """Save current state of ``block_matcher``."""
        for parameter in self.block_matcher.parameter_maxima.keys():
            self.bm_settings[parameter].append(
                               self.block_matcher.__getattribute__(parameter))
    def update_disparity_map(self):
        """
        Update disparity map in GUI.

        The disparity image is normalized to the range 0-255 and then divided by
        255, because OpenCV multiplies it by 255 when displaying. This is
        because the pixels are stored as floating points.
        """
        disparity = self.block_matcher.compute_disparity(self.pair)
        norm_coeff = 255 / disparity.max()
        cv2.imshow(self.window_name, disparity * norm_coeff / 255)
        cv2.waitKey()
    def tune_pair(self, pair):
        """Tune a pair of images."""
        self._save_bm_state()
        self.pair = pair
        self.update_disparity_map()
    def report_settings(self, parameter):
        """
        Report chosen settings for ``parameter`` in ``block_matcher``.

        ``bm_settings`` is updated to include the latest value before work is
        begun. This value is removed at the end so that the method has no side
        effects. All settings are reported except for the first one on record,
        which is the ``BlockMatcher``'s default setting.
        """
        self._save_bm_state()
        report = []
        settings_list = self.bm_settings[parameter][1:]
        unique_values = list(set(settings_list))
        value_frequency = {}
        for value in unique_values:
            value_frequency[settings_list.count(value)] = value
        frequencies = value_frequency.keys()
        frequencies.sort(reverse=True)
        header = "{} value | Selection frequency".format(parameter)
        left_column_width = len(header[:-21])
        right_column_width = 21
        report.append(header)
        report.append("{}|{}".format("-" * left_column_width,
                                    "-" * right_column_width))
        for frequency in frequencies:
            left_column = str(value_frequency[frequency]).center(
                                                             left_column_width)
            right_column = str(frequency).center(right_column_width)
            report.append("{}|{}".format(left_column, right_column))
        # Remove newest settings
        for param in self.block_matcher.parameter_maxima.keys():
            self.bm_settings[param].pop(-1)
        return "\n".join(report)

STEREO_SGBM_FLAG = ArgumentParser(add_help=False)
STEREO_SGBM_FLAG.add_argument("--use_stereobm", help="Use StereoBM rather than "
                              "StereoSGBM block matcher.", action="store_true")

def main():
    """
    Let user tune all images in the input folder and report chosen values.

    Load a calibration from file and instantiate a ``BlockMatcher`` of the type
    requested by the user. Load images successively from input folder and
    display their resultant disparity map generated with the ``BlockMatcher``
    and the parameters chosen in the ``BMTuner``'s GUI. Afterwards, report
    user's chosen settings and, if a file for the BM settings is provided, save
    the most common settings to file.
    """
    parser = ArgumentParser(description="Read images taken from a calibrated "
                           "stereo pair, compute disparity maps from them and "
                           "show them interactively to the user, allowing the "
                           "user to tune the stereo block matcher settings in "
                           "the GUI.", parents=[STEREO_SGBM_FLAG])
    parser.add_argument("calibration_folder",
                        help="Directory where calibration files for the stereo "
                        "pair are stored.")
    parser.add_argument("image_folder",
                        help="Directory where input images are stored.")
    parser.add_argument("--bm_settings",
                        help="File to save last block matcher settings to.",
                        default="")
    args = parser.parse_args()

    calibration = StereoCalibration(input_folder=args.calibration_folder)
    input_files = find_files(args.image_folder)
    if args.use_stereobm:
        block_matcher = StereoBM()
    else:
        block_matcher = StereoSGBM()
    image_pair = [cv2.imread(image) for image in input_files[:2]]
    input_files = input_files[2:]
    rectified_pair = calibration.rectify(image_pair)
    imshow_cv('rectified', np.hstack(list(rectified_pair)))
    imshow_cv('left', rectified_pair[0])
    imshow_cv('right', rectified_pair[1])

    tuner = BMTuner(block_matcher, calibration, rectified_pair)

    while input_files:
        image_pair = [cv2.imread(image) for image in input_files[:2]]
        # rectified_pair = calibration.rectify([image_pair[1], image_pair[0]])
        tuner.tune_pair(rectified_pair)
        input_files = input_files[2:]

    for param in block_matcher.parameter_maxima:
        print("{}\n".format(tuner.report_settings(param)))

    if args.bm_settings:
        block_matcher.save_settings(args.bm_settings)

if __name__ == "__main__":
    main()
