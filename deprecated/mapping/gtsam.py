"""SLAM interface with GTSAM"""

# Author: Sudeep Pillai <spillai@csail.mit.edu>
# License: MIT

import sys
import numpy as np
np.set_printoptions(precision=2, suppress=True)

from collections import deque, defaultdict, Counter, namedtuple
from itertools import izip
from threading import Lock, RLock

from pybot.geometry.rigid_transform import RigidTransform
from pybot.utils.timer import SimpleTimer, timeitmethod
from pybot.utils.db_utils import AttrDict
from pybot.utils.misc import print_red, print_yellow, print_green
from pybot.mapping import cfg

from pygtsam import Symbol, extractPose2, extractPose3, extractPoint3, extractKeys
from pygtsam import symbol as _symbol
from pygtsam import Point2, Rot2, Pose2, \
    PriorFactorPose2, BetweenFactorPose2, \
    BearingRangeFactorPose2Point2
from pygtsam import Point3, Rot3, Pose3, \
    PriorFactorPose3, BetweenFactorPose3, PriorFactorPoint3
from pygtsam import SmartFactor
from pygtsam import Cal3_S2, SimpleCamera, simpleCamera
from pygtsam import StereoPoint2, Cal3_S2Stereo, \
    GenericStereoFactor3D, GenericProjectionFactorPose3Point3Cal3_S2
from pygtsam import NonlinearEqualityPose3
from pygtsam import Isotropic
from pygtsam import Diagonal, Values, Marginals
from pygtsam import ISAM2Params
from pygtsam import ISAM2, NonlinearOptimizer, \
    NonlinearFactorGraph, LevenbergMarquardtOptimizer, DoglegOptimizer

# Externals
from pybot_gtsam import BetweenFactorMaxMixPose3
from pybot_gtsam import SwitchVariableSigmoid, PriorFactorSwitchVariableSigmoid, \
    BetweenFactorSwitchableSigmoidPose3, extractSwitchVariableSigmoid


def symbol(ch, i): 
    return _symbol(ord(ch), i)

def vector(v): 
    return np.float64(v)

def matrix(m): 
    return np.float64(m)

def vec(*args):
    return vector(list(args)) 

GTSAMException = namedtuple('GTSAMException', ['message', 'symbol'])
def get_exception_variable(msg, print_message=False):
    """ 
    Catch exception and print relevant gtsam symbol
    """
    try:
        split = msg.split('\n')
        s = Symbol(int(split[2][:-1]))
        custom_message = '{} (Symbol: {} {})'.format(split[1], chr(s.chr()), s.index())
        if print_message:
            custom_message = custom_message + '{}\n'.format(msg)
    except:
        s, custom_message = None, msg
    return GTSAMException(custom_message, s)

class BaseSLAM(object):
    """
    BASIC SLAM interface with GTSAM::ISAM2

    This is a basic interface that allows hot-swapping factors without
    having to write much boilerplate and templated code.
    
    Factor graph is constructed and grown dynamically, with
    Pose3-Pose3 constraints, and finally optimized.

    Params: 
        xs: Robot poses
        ls: Landmark measurements
        xls: Edge list between x and l 

    Todo: 
        - Updated slam every landmark addition
        - Support for switching between Pose2/Pose3
        - Max-Mixtures / Switchable constraints
          See vertigo/examples/robustISAM2/robustISAM2.cpp
          See self.robust_ = False

    """
    def __init__(self, 
                 odom_noise=cfg.ODOM_NOISE, 
                 prior_pose_noise=cfg.PRIOR_POSE_NOISE, 
                 measurement_noise=cfg.MEASUREMENT_NOISE,
                 robust=True, verbose=False, export_graph=False):
 
        # ISAM2 interface
        self.slam_ = ISAM2()
        self.slam_lock_ = Lock()
        
        self.idx_ = -1
        self.verbose_ = verbose
        
        # Factor graph storage
        self.graph_ = NonlinearFactorGraph()
        self.initial_ = Values()
        
        # Pose3D measurement
        self.measurement_noise_ = Diagonal.Sigmas(measurement_noise)
        self.prior_pose_noise_ = Diagonal.Sigmas(prior_pose_noise)
        self.odo_noise_ = Diagonal.Sigmas(odom_noise)

        # Optimized robot state
        self.state_lock_ = Lock()
        self.xs_ = {}
        self.ls_ = {}
        self.xls_ = []
        self.xxs_ = []

        self.xcovs_ = {}
        self.lcovs_ = {}
        self.current_ = None

        # Robust
        self.robust_ = True # TODO/FIX handle properly
        self.switch_ = []

    @property
    def pretty_name(self):
        return 'GTSAM_{}'.format(self.__class__.__name__)
        
    def initialize(self, p=RigidTransform.identity(), index=0, noise=None): 
        if self.verbose_:
            print_red('{}::initialize index: {}={}'
                      .format(self.__class__.__name__, index, p))
            print_red('{:}::add_pose_prior {}={}'
                      .format(self.pretty_name, index, p))
            
        x_id = symbol('x', index)
        pose0 = Pose3(p.matrix)
        self.graph_.add(
            PriorFactorPose3(x_id, pose0,
                             Diagonal.Sigmas(noise)
                             if noise is not None
                             else self.prior_pose_noise_)
        )
        self.initial_.insert(x_id, pose0)
        with self.state_lock_: 
            self.xs_[index] = p
        self.idx_ = index

    def add_pose_prior(self, index, p, noise=None): 
        if self.verbose_:
            print_red('{:}::add_pose_prior {}={}'
                      .format(self.pretty_name, index, p))
        x_id = symbol('x', index)
        pose = Pose3(p.matrix)
        self.graph_.add(
            PriorFactorPose3(x_id, pose, Diagonal.Sigmas(noise)
                             if noise is not None
                             else self.prior_pose_noise_)
        )
        
    def add_incremental_pose_constraint(self, delta, noise=None): 
        """
        Add odometry measurement from the latest robot pose to a new
        robot pose
        """
        # Add prior on first pose
        if not self.is_initialized:
            self.initialize()

        # Add odometry factor
        self.add_relative_pose_constraint(self.latest, self.latest+1, delta, noise=noise)
        self.idx_ += 1
        
    def add_relative_pose_constraint(self, xid1, xid2, delta, noise=None): 
        if self.verbose_:
            print_red('{}::add_odom {}->{} = {}'
                      .format(self.pretty_name, xid1, xid2, delta))

        # Add odometry factor
        pdelta = Pose3(delta.matrix)
        x_id1, x_id2 = symbol('x', xid1), symbol('x', xid2)

        # Robust (Switchable constraints / Max-mixtures)
        if self.robust_: 
            # Create new switch variable
            switch_idx = len(self.switch_)
            self.switch_.append(1.)
            self.initial_.insert(symbol('s', switch_idx), SwitchVariableSigmoid(1.))

            # Create switch prior factor
            sw_prior_model = Diagonal.Sigmas(vec(2.))
            sw_prior_factor = PriorFactorSwitchVariableSigmoid(symbol('s', switch_idx),
                                                 SwitchVariableSigmoid(1.), sw_prior_model)
            self.graph_.add(sw_prior_factor)

            # Create switchable odometry factor
            odom_model = Diagonal.Sigmas(noise) \
                         if noise is not None else self.odo_noise_
            sw_factor = BetweenFactorSwitchableSigmoidPose3(x_id1, x_id2,
                                                            symbol('s', switch_idx),
                                                            pdelta, odom_model)
            self.graph_.add(sw_factor)

        # If not robust (add as is)
        else:
            self.graph_.add(BetweenFactorPose3(x_id1, x_id2, 
                                               pdelta, Diagonal.Sigmas(noise)
                                               if noise is not None
                                               else self.odo_noise_))

        # Max-Mixtures factor
        # null_weight = 0.25
        # odom_noise = Diagonal.Sigmas(noise)
        # null_noise = Diagonal.Sigmas(noise / null_weight)
        # self.graph_.add(BetweenFactorMaxMixPose3(
        #     x_id1, x_id2, pdelta,
        #     odom_noise, null_noise, null_weight))

        
        # Predict pose and add as initial estimate
        # TODO: self.xs_[xid1] may not be most recent,
        # use calculateEstimate(xid1) to get the most
        # recent pose (optimized/updated potentially)
        with self.state_lock_: 
            if xid2 not in self.xs_: 
                pred_pose = self.xs_[xid1].oplus(delta)
                self.initial_.insert(x_id2, Pose3(pred_pose.matrix))
                self.xs_[xid2] = pred_pose

            # Add to edges
            self.xxs_.append((xid1, xid2))

    def add_pose_landmarks(self, xid, lids, deltas, noise=None): 
        if self.verbose_: 
            print_red('{:}::add_landmark x{:} -> lcount: {:}'
                      .format(self.pretty_name, xid, len(lids)))

        # Add Pose-Pose landmark factor
        x_id = symbol('x', xid)
        l_ids = [symbol('l', lid) for lid in lids]
        
        # Add landmark poses
        noise = Diagonal.Sigmas(noise) if noise is not None \
                else self.measurement_noise_
        assert(len(l_ids) == len(deltas))
        for l_id, delta in izip(l_ids, deltas): 
            self.graph_.add(BetweenFactorPose3(x_id, l_id, Pose3(delta.matrix), noise))

        with self.state_lock_: 

            # Add to landmark measurements
            self.xls_.extend([(xid, lid) for lid in lids])

            # Initialize new landmark pose node from the latest robot
            # pose. This should be done just once

            # TODO: self.xs_[xid1] may not be most recent,
            # use calculateEstimate(xid1) to get the most
            # recent pose (optimized/updated potentially)

            for (l_id, lid, delta) in izip(l_ids, lids, deltas): 
                if lid not in self.ls_:
                    try: 
                        pred_pose = self.xs_[xid].oplus(delta)
                        self.initial_.insert(l_id, Pose3(pred_pose.matrix))
                        self.ls_[lid] = pred_pose                        
                    except: 
                        raise KeyError('Pose {:} not available'
                                       .format(xid))
            
        return 

    def add_pose_landmarks_incremental(self, lid, delta, noise=None): 
        """
        Add landmark measurement (pose3d) 
        from the latest robot pose to the
        specified landmark id
        """
        self.add_pose_landmarks(self.latest, lid, delta, noise=noise)

    def add_point_landmarks(self, xid, lids, pts, pts3d, noise=None): 
        if self.verbose_: 
            print_red('add_landmark_points xid:{:}-> lid count:{:}'
                      .format(xid, len(lids)))
        
        # Add landmark-ids to ids queue in order to check
        # consistency in matches between keyframes. This 
        # allows an easier interface to check overlapping 
        # ids across successive function calls.
        self.lid_count_ += Counter(lids)

        # if len(ids_q_) < 2: 
        #     return

        # Add Pose-Pose landmark factor
        x_id = symbol('x', xid)
        l_ids = [symbol('l', lid) for lid in lids]

        noise = Diagonal.Sigmas(noise) if noise is not None \
                else self.image_measurement_noise_
        assert(len(l_ids) == len(pts) == len(pts3d))
        for l_id, pt in izip(l_ids, pts):
            self.graph_.add(
                GenericProjectionFactorPose3Point3Cal3_S2(
                    Point2(vec(*pt)), noise, x_id, l_id, self.K_))

        with self.state_lock_: 

            # # Add to landmark measurements
            # self.xls_.extend([(xid, lid) for lid in lids])

            # Initialize new landmark pose node from the latest robot
            # pose. This should be done just once
            for (l_id, lid, pt3) in izip(l_ids, lids, pts3d): 
                if lid not in self.ls_: 
                    try: 
                        pred_pt3 = self.xs_[xid].transform_from(Point3(vec(*pt3)))
                        self.initial_.insert(l_id, pred_pt3)
                        self.ls_[lid] = pred_pt3.vector().ravel()
                    except Exception, e: 
                        raise RuntimeError('Initialization failed ({:}). xid:{:}, lid:{:}, l_id: {:}'
                                           .format(e, xid, lid, l_id))
        
        return 

    def add_point_landmarks_incremental(self, lids, pts, pts3d, noise=None): 
        """
        Add landmark measurement (image features)
        from the latest robot pose to the
        set of specified landmark ids
        """
        self.add_point_landmarks(self.latest, lids, pts, pts3d, noise=noise)

    @timeitmethod
    def _update(self, iterations=1): 
        # print('.')
        # print('_update {}'.format(self.idx_))
        
        # Update ISAM with new nodes/factors and initial estimates
        try: 
            self.slam_.update(self.graph_, self.initial_)
            self.slam_.update()
                
        except Exception, e:
            s = get_exception_variable(e.message); print(s)
            import IPython; IPython.embed()
            raise RuntimeError()

        # Graph and initial values cleanup
        # print self.graph_.printf()
        self.graph_.resize(0)
        self.initial_.clear()
        
        # Get current estimate
        self.current_ = self.slam_.calculateEstimate()
            
    def _batch_solve(self):
        " Optimize using Levenberg-Marquardt optimization "
        
        # with self.slam_lock_:
        opt = LevenbergMarquardtOptimizer(self.graph_, self.initial_)
        self.current_ = opt.optimize()
        
    @timeitmethod
    def _update_estimates(self): 
        if not self.estimate_available:
            raise RuntimeError('Estimate unavailable, call update first')

        poses = extractPose3(self.current_)
        landmarks = extractPoint3(self.current_)
        switches = extractSwitchVariableSigmoid(self.current_)
        
        with self.state_lock_: 
            # Extract and update landmarks and poses
            for k,v in poses.iteritems():
                if chr(k.chr()) == 'l': 
                    self.ls_[k.index()] = RigidTransform.from_matrix(v.matrix())
                elif chr(k.chr()) == 'x': 
                    self.xs_[k.index()] = RigidTransform.from_matrix(v.matrix())
                else: 
                    raise RuntimeError('Unknown key chr {} {}'.format(chr(k.chr()), k.index()))

            # Extract and update landmarks
            for k,v in landmarks.iteritems():
                if chr(k.chr()) == 'l': 
                    self.ls_[k.index()] = v.vector().ravel()
                else: 
                    raise RuntimeError('Unknown key chr {} {}'.format(chr(k.chr()), k.index()))

            # Extract/Update switches
            for k,v in switches.iteritems():
                self.switch_[k.index()] = v.value()
            
        # self.cleanup()
        
        # if self.index % 10 == 0 and self.index > 0: 
        # self.save_graph("slam_fg.dot")
        # self.save_dot_graph("slam_graph.dot")

    def _update_marginals(self): 
        if not self.estimate_available:
            raise RuntimeError('Estimate unavailable, call update first')

        # Retrieve marginals for each of the poses
        # with self.slam_lock_: 
        for xid in self.xs_: 
            self.xcovs_[xid] = self.slam_.marginalCovariance(symbol('x', xid))

        for lid in self.ls_: 
            self.lcovs_[lid] = self.slam_.marginalCovariance(symbol('l', lid))
    
    @property
    def latest(self): 
        return self.idx_

    @property
    def index(self): 
        return self.idx_

    @property
    def is_initialized(self): 
        return self.latest >= 0

    @property
    def poses_count(self): 
        " Robot poses: Expects poses to be Pose3 "
        return len(self.xs_)

    @property
    def poses(self): 
        " Robot poses: Expects poses to be Pose3 "
        return self.xs_ # {k: v.matrix() for k,v in self.xs_.iteritems()}

    def pose(self, k): 
        return self.xs_[k].matrix()
        
    @property
    def target_poses(self): 
        " Landmark Poses: Expects landmarks to be Pose3 "
        return self.ls_ # {k: v.matrix() for k,v in self.ls_.iteritems()}

    @property
    def target_poses_count(self): 
        " Landmark Poses: Expects landmarks to be Pose3 "
        return len(self.ls_)

    def target_pose(self, k): 
        return self.ls_[k] # .matrix()
        
    @property
    def target_landmarks(self): 
        " Landmark Points: Expects landmarks to be Point3 " 
        return self.ls_ # {k: v.vector().ravel() for k,v in self.ls_.iteritems()}

    @property
    def target_landmarks_count(self): 
        " Landmark Points: Expects landmarks to be Point3 " 
        return len(self.ls_)

    def target_landmark(self, k): 
        return self.ls_[k].vector().ravel()

    @property
    def poses_marginals(self): 
        " Marginals for Robot poses: Expects poses to be Pose3 "
        return self.xcovs_ 
        
    @property
    def target_poses_marginals(self): 
        " Marginals for Landmark Poses: Expects landmarks to be Pose3 "
        return self.lcovs_
        
    @property
    def target_landmarks_marginals(self): 
        " Marginals for Landmark Points: Expects landmarks to be Point3 " 
        return self.lcovs_

    def pose_marginal(self, node_id): 
        return self.xcovs_[node_id]

    def landmark_marginal(self, node_id): 
        return self.lcovs_[node_id]
        
    @property
    def landmark_edges(self): 
        return self.xls_

    @property
    def robot_edges(self): 
        return self.xxs_

    @property
    def robot_edges_confident(self): 
        return np.float32(self.switch_) if self.robust_ \
            else np.ones(len(self.xxs_))

    @property
    def estimate_available(self): 
        return self.current_ is not None

    @property
    def marginals_available(self): 
        return len(self.xcovs_) > 0 or len(self.lcovs_) > 0

    def save_graph(self, filename):
        # with self.slam_lock_: 
        self.slam_.saveGraph(filename)

def two_view_BA(K, pts1, pts2, X, p_21, scale_prior=True):

    # Define the camera calibration parameters
    # format: fx fy skew cx cy
    K = Cal3_S2(K.fx, K.fy, 0.0, K.cx, K.cy)
    X = X.astype(np.float64)
    infront = X[:,2] >= 0

    # Only perform BA on points in front
    pts1, pts2, X = pts1[infront], pts2[infront], X[infront]    
    
    # Create a factor graph
    graph = NonlinearFactorGraph()

    px_noise = [1., 1.]
    measurement_noise = Diagonal.Sigmas(vec(*px_noise))

    # Add a prior on pose x0
    prior_pose_noise = Diagonal.Sigmas(vec(0.1, 0.1, 0.1, 0.05, 0.05, 0.05))
    graph.add(PriorFactorPose3(symbol('x', 0), Pose3(), prior_pose_noise))

    # Add relative pose constraint between x0-x1
    pdelta = Pose3(p_21.matrix)
    odo_noise = Diagonal.Sigmas(np.ones(6) * 0.1)
    graph.add(BetweenFactorPose3(symbol('x', 0), symbol('x', 1),
                                 pdelta, odo_noise))
              
    # Add prior on first landmark (scale prior for monocular case)
    point_noise = Diagonal.Sigmas(np.ones(3) * 0.05)
    for j in range(max(5, len(X))): 
        point = Point3(X[j,:].ravel())
        graph.add(PriorFactorPoint3(symbol('l', j), point, point_noise))
    
    # Add image measurements
    for lid, (pt1,pt2) in enumerate(izip(pts1, pts2)):
        graph.add(
            GenericProjectionFactorPose3Point3Cal3_S2(
                Point2(vec(*pt1)), measurement_noise, symbol('x', 0), symbol('l', lid), K))

        graph.add(
            GenericProjectionFactorPose3Point3Cal3_S2(
                Point2(vec(*pt2)), measurement_noise, symbol('x', 1), symbol('l', lid), K))

    
    # Create the initial estimate to the solution
    # Intentionally initialize the variables off from the ground truth
    initialEstimate = Values()
    # delta = Pose3(Rot3.rodriguez(-0.1, 0.2, 0.25), Point3(0.05, -0.10, 0.20))
    initialEstimate.insert(symbol('x', 0), Pose3())
    initialEstimate.insert(symbol('x', 1), Pose3(p_21.matrix))

    # Insert intial estimates for landmark
    for lid, pt in enumerate(X):
        initialEstimate.insert(symbol('l', lid), Point3(pt.ravel()))
    
    # Optimize the graph and print results
    try: 
        result = DoglegOptimizer(graph, initialEstimate).optimize()
        result.printf("Final results:\n")
    except Exception, e:
        s = get_exception_variable(e.message); print(s)
        import IPython; IPython.embed()
        raise RuntimeError()

    print 'Original pose: ', p_21
    print('\nBA SUCCESSFUL\n' + '=' * 80)
    import IPython; IPython.embed()
        
class VisualSLAM(BaseSLAM): 
    def __init__(self, calib, min_landmark_obs=cfg.VSLAM_MIN_LANDMARK_OBS,
                 odom_noise=cfg.ODOM_NOISE, prior_pose_noise=cfg.PRIOR_POSE_NOISE,
                 prior_point3d_noise=cfg.PRIOR_POINT3D_NOISE, 
                 px_error_threshold=4, px_noise=cfg.PX_MEASUREMENT_NOISE, verbose=False):
        BaseSLAM.__init__(self, odom_noise=odom_noise, prior_pose_noise=prior_pose_noise, verbose=verbose)
        self.batch_init_ = False

        # Set relinearizationskip, and factorization method
        # params = ISAM2Params()
        # params.setRelinearizeSkip(1)
        # params.setFactorization('CHOLESKY')
        # self.slam_ = ISAM2(params)
        
        self.px_error_threshold_ = px_error_threshold
        self.min_landmark_obs_ = min_landmark_obs
        assert(self.min_landmark_obs_ >= 2)

        # Define the camera calibration parameters
        # format: fx fy skew cx cy
            
        # Calibration for specific instance
        # that is maintained across the entire
        # pose-graph optimization (assumed static)
        self.K_ = Cal3_S2(calib.fx, calib.fy, 0.0, calib.cx, calib.cy)
        
        # Counter for landmark observations
        self.lid_count_ = Counter()
            
        # Dictionary pointing to smartfactor set
        # for each landmark id
        # self.lid_factors_ = defaultdict(SmartFactor)
        # self.lid_factors_ = defaultdict(lambda: dict(
        #     in_graph=False, factor=SmartFactor(rankTol=1, linThreshold=-1, manageDegeneracy=False)))
        self.lid_factors_ = defaultdict(
            lambda: AttrDict(in_graph=False,
                             count=0, 
                             factor=SmartFactor(rankTol=1, linThreshold=-1,
                                                manageDegeneracy=False, body_P_sensor=None,
                                                landmarkDistanceThreshold=100)))
        
        # Measurement noise (2 px in u and v)
        self.image_measurement_noise_ = Diagonal.Sigmas(vec(*px_noise))
        self.prior_point3d_noise_ = Diagonal.Sigmas(prior_point3d_noise)

    def add_landmark_prior(self, index, p, noise=None): 
        if self.verbose_:
            print_red('{:}::add_landmark_prior {}={}'
                      .format(self.pretty_name, index, p))
        l_id = symbol('l', index)
        point = Point3(p)
        self.graph_.add(
            PriorFactorPoint3(l_id, point, Diagonal.Sigmas(noise)
                             if noise is not None
                             else self.prior_point3d_noise_)
        )

    def add_point_landmarks_incremental_smart(self, lids, pts, keep_tracked=True): 
        """
        Add landmark measurement (image features)
        from the latest robot pose to the
        set of specified landmark ids
        """
        self.add_point_landmarks_smart(self.latest, lids, pts, keep_tracked=keep_tracked)

    @timeitmethod
    def add_point_landmarks_smart(self, xid, lids, pts, keep_tracked=True): 
        """
        keep_tracked: Maintain only tracked measurements in the smart factor list; 
        The alternative is that all measurements are added to the smart factor list
        """
        if self.verbose_: 
            print_red('{:}::add_landmark_points_smart {:}->{:}'
                      .format(self.pretty_name, xid, lids))
        
        # Mahalanobis check before adding points to the 
        # factor graph
        # self.check_point_landmarks(xid, lids, pts)
        
        # Add landmark-ids to ids queue in order to check 
        # consistency in matches between keyframes. This 
        # allows an easier interface to check overlapping 
        # ids across successive function calls.

        # # Only maintain lid counts for previously tracked 
        # for lid in self.lid_count_.keys():
        #     if lid not in self.lid_factors_: 
        #         self.lid_count_.pop(lid)

        # # Add new tracks to the counter
        # self.lid_count_ += Counter(lids)

        # Add Pose-Pose landmark factor
        x_id = symbol('x', xid)
        l_ids = [symbol('l', lid) for lid in lids]
        
        assert(len(l_ids) == len(pts))
        if self.verbose_: 
            print_yellow('Adding landmark measurement: x{} -> '.format(xid))
            
        for (lid, l_id, pt) in izip(lids, l_ids, pts):

            # If the landmark is already initialized, 
            # then add to graph
            if lid in self.ls_:

                if self.verbose_: 
                    sys.stdout.write('a{},'.format(lid))

                # # Add projection factors 
                # self.graph_.add(GenericProjectionFactorPose3Point3Cal3_S2(
                #     Point2(vec(*pt)), self.image_measurement_noise_, x_id, l_id, self.K_))
                
                # # Add to landmark measurements
                # self.xls_.append((xid, lid))

            # In case the landmarks have not been initialized, add 
            # as a smart factor and delay until multiple views have
            # been registered
            else: 
                if self.verbose_: 
                    sys.stdout.write('s{},'.format(lid))
                # print_yellow('{} '.format(lid))

                # Insert smart factor based on landmark id
                self.lid_factors_[lid].factor.add_single(
                    Point2(vec(*pt)), x_id, self.image_measurement_noise_, self.K_
                )
                self.lid_factors_[lid].count += 1

        if self.verbose_: 
            sys.stdout.write('\n')
            sys.stdout.flush()
            
        # # Keep only successively tracked features
        # if not keep_tracked: 
        #     # Add smartfactors to the graph only if that 
        #     # landmark ID is no longer visible. setdiff1d
        #     # returns the set of IDs that are unique to 
        #     # `smart_lids` (previously tracked) but not 
        #     # in `lids` (current)
        #     smart_lids = np.int64(self.lid_factors_.keys())

        #     # Determine old lids that are no longer tracked and add
        #     # only the ones that have at least min_landmark_obs
        #     # observations. Delete old factors that have insufficient
        #     # number of observations

        #     dropped_lids = np.setdiff1d(smart_lids, lids)
        #     for lid in dropped_lids:
        #         self.lid_factors_.pop(lid)

        if self.verbose_: 
            self.print_stats()
        return 

    def print_stats(self): 
        print_red('\tLID factors: {}'.format(len(self.lid_factors_)))

    @timeitmethod
    def smart_update(self, delete_factors=True): 
        """
        Update the smart factors and add 
        to the graph. Once the landmarks are 
        extracted, remove them from the factor list
        """
        current = self.slam_.calculateEstimate()

        lids, pts3 = [], []
        if self.verbose_: 
            print_yellow('smart_update()')

        for lid in self.lid_factors_.keys(): 

            # No need to initialize smart factor if already 
            # added to the graph OR 
            # Cannot incorporate factor without sufficient observations
            lid_factor = self.lid_factors_[lid]
            if lid_factor.in_graph or lid_factor.count < self.min_landmark_obs_:
                continue

            l_id = symbol('l', lid)
            smart = lid_factor.factor

            # Cannot do much when degenerate or behind camera
            if smart.isDegenerate() or smart.isPointBehindCamera():
                # Delete smart lid factor and key,val pair
                del smart
                del self.lid_factors_[lid]
                continue

            # Check smartfactor reprojection error 
            err = smart.error(current)
            print lid, err
            if err > self.px_error_threshold_ or err <= 0.0:
                del smart
                del self.lid_factors_[lid]
                continue

            # Initialize the point value, set in_graph, and
            # remove the smart factor once point is computed
            pt3 = smart.point_compute(current)
            
            # Provide initial estimate to factor graph
            assert(lid not in self.ls_)
            if lid not in self.ls_: 
                self.initial_.insert(l_id, pt3)
                self.ls_[lid] = pt3.vector().ravel()
                if self.verbose_: 
                    sys.stdout.write('il{}, '.format(lid))
            else:
                assert(0)

            # Add the points for visualization 
            lids.append(lid)
            pts3.append(pt3.vector().ravel())

            # Add triangulated smart factors back into the graph for
            # complete point-pose optimization Each of the projection
            # factors, including the points, and their initial values
            # are added back to the graph. Optionally, we can choose
            # to subsample and add only a few measurements from the
            # set of original measurements

            x_ids = smart.keys()
            pts = smart.measured()
            assert len(pts) == len(x_ids)
            # print len(pts), len(x_ids)
            
            # Add each of the smart factor measurements to the 
            # factor graph
            if self.verbose_: 
                sys.stdout.write('[ l{} -> '.format(lid))
            for x_id,pt in zip(x_ids, pts):
                pass
                # if self.verbose_: 
                #     sys.stdout.write('a{},'.format(Symbol(x_id).index()))

                self.graph_.add(GenericProjectionFactorPose3Point3Cal3_S2(
                    pt, self.image_measurement_noise_, x_id, l_id, self.K_))
                
                # # Add to landmark measurements
                # self.xls_.append((Symbol(x_id).index(), lid))
            if self.verbose_: 
                sys.stdout.write(' ], \n')

            # Delete smart lid factor and key,val pair
            del smart
            del self.lid_factors_[lid]

        if self.verbose_: 
            sys.stdout.write('\n')
            sys.stdout.flush()
            
            # if delete_factors: 
            #     # Once all observations are incorporated, 
            #     # remove feature altogether. Can be deleted
            #     # as long as the landmarks are initialized
            #     self.lid_count_.pop(lid)


        # Add landmark priors to first set of landmarks
        print 'err threshold:', self.px_error_threshold_
        print 'Smart update index: {}, # landmarks: {}'.format(self.index, lids)
        if self.index <= 2:
            
            for lid, pt3 in izip(lids, pts3): 
                self.add_landmark_prior(lid, pt3)
                
        if self.verbose_: 
            print_yellow('smart_update() DONE')
            
        try: 
            lids, pts3 = np.int64(lids).ravel(), np.vstack(pts3)            
            assert(len(lids) == len(pts3))
            return lids, pts3
        except Exception, e:
            # print('Could not return pts3, {:}'.format(e))
            return np.int64([]), np.array([])        

    @timeitmethod
    def _update(self, iterations=1): 
        # print('.')
        # print('_update {}'.format(self.idx_))
        
        # Update ISAM with new nodes/factors and initial estimates
        try: 

            # Do a full optimization on the first two poses,
            # before performing updates for subsequent calls
            if not self.batch_init_:
                self.batch_init_ = True
                opt = LevenbergMarquardtOptimizer(self.graph_, self.initial_)
                current = opt.optimize()
                self.slam_.update(self.graph_, current)

                # print 'Current:'
                # current.printf()
                
            # Update with estimates
            # TODO: (iterate ?)
            else: 
                self.slam_.update(self.graph_, self.initial_)
                self.slam_.update()
                
        except Exception, e:
            s = get_exception_variable(e.message); print(s)
            import IPython; IPython.embed()
            raise RuntimeError()

        # Graph and initial values cleanup
        # print self.graph_.printf()
        self.graph_.resize(0)
        self.initial_.clear()
        
        # Get current estimate
        self.current_ = self.slam_.calculateEstimate()

    # def check_point_landmarks(self, xid, lids, pts): 
    #     print_red('{:}::check_point_landmarks {:}->{:}, ls:{:}'.format(
    #         self.pretty_name, xid, len(lids), len(self.ls_)))


    #     print 'new landmark', xid, lids

    #     if not len(self.ls_): 
    #         return

    #     # Recover pose
    #     camera = SimpleCamera(self.xs_[xid], self.K_)
    #     # Project landmarks onto current frame, 
    #     # and check mahalanobis distance
    #     for (pt, lid) in izip(pts, lids): 
    #         if lid not in self.ls_: 
    #             continue

    #         print 'new landmark', xid, lid
        

    #         # Project feature onto camera and check
    #         # distance metric
    #         pred_pt = camera.project(self.ls_[lid])
    #         print('LID {:} Distance {:}'.format(lid, pred_pt.vector().ravel()-pt))
            
    #         # pred_cov = camera.project(self.lcovs_[lid])
    #         # mahalanobis_distance(pt, pred_pt)

