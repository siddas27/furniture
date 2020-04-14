import os
from typing import Tuple

import mujoco_py
import numpy as np
from gym.envs.mujoco import mujoco_env

from env.action_spec import ActionSpec
from env.base import EnvMeta
from util.demo_recorder import DemoRecorder


class PegInsertionEnv(mujoco_env.MujocoEnv, metaclass=EnvMeta):
    """PegInsertionEnv
    Extends https://github.com/brain-research/LeaveNoTrace
    We define the forward task to be pulling the peg out of the hole, and the
    reset task to be putting the peg into the hole.
    """

    def __init__(self, config):
        self._sparse = config.sparse_rew
        self._task = config.task
        self.name = "Peg" + self._task.capitalize()
        self._max_episode_steps = config.max_episode_steps
        self._robot_ob = config.robot_ob
        self._goal_pos_threshold = config.goal_pos_threshold
        self._record_demo = config.record_demo
        self._goal_type = config.goal_type
        self._action_noise = config.action_noise

        # reward config
        self._peg_to_point_rew_coeff = config.peg_to_point_rew_coeff
        self._success_rew = config.success_rew
        self._control_penalty_coeff = config.control_penalty_coeff

        # demo loader
        if self._record_demo:
            self._demo = DemoRecorder(config.demo_dir)

        envs_folder = os.path.dirname(os.path.abspath(__file__))
        xml_filename = os.path.join(envs_folder, "models/assets/peg_insertion.xml")
        self._initialize_mujoco(xml_filename, 5)
        self._reset_episodic_vars()

    def _initialize_mujoco(self, model_path, frame_skip):
        """Taken from mujoco_env.py __init__ from mujoco_py package"""
        if model_path.startswith("/"):
            fullpath = model_path
        else:
            fullpath = os.path.join(os.path.dirname(__file__), "assets", model_path)
        self.frame_skip = frame_skip
        self.model = mujoco_py.load_model_from_path(fullpath)
        self.sim = mujoco_py.MjSim(self.model)
        self.data = self.sim.data
        self.viewer = None
        self._viewers = {}

        self.metadata = {
            "render.modes": ["human", "rgb_array", "depth_array"],
            "video.frames_per_second": int(np.round(1.0 / self.dt)),
        }

        self.init_qpos = self.sim.data.qpos.ravel().copy()
        self.init_qvel = self.sim.data.qvel.ravel().copy()
        self.seed()

    def step(self, a) -> Tuple[dict, float, bool, dict]:
        if isinstance(a, dict):
            a = np.concatenate([a[key] for key in self.action_space.shape.keys()])

        if self._action_noise is not None:
            r = self._action_noise
            a = a + self.np_random.uniform(-r, r, size=len(a))

        self.do_simulation(a, self.frame_skip)
        done = False
        obs = self._get_obs()
        self._episode_length += 1
        if self._task == "insert":
            reward, info = self._insert_reward(obs, a)
        elif self._task == "remove":
            reward, info = self._remove_reward(obs, a)
        self._episode_reward += reward

        if self._success or self._episode_length == self._max_episode_steps:
            done = True
            info["episode_reward"] = self._episode_reward
            info["episode_success"] = int(self._success)

        info["reward"] = reward
        if self._record_demo:
            self._demo.add(ob=obs, action=a, reward=reward)
        return obs, reward, done, info

    def reset(self, **kwargs):
        ob = super().reset()
        self._reset_episodic_vars()
        if self._record_demo:
            self._demo.add(ob=ob)
        return ob

    def viewer_setup(self):
        self.viewer.cam.trackbodyid = -1
        self.viewer.cam.distance = 4.0

    def _reset_episodic_vars(self):
        """
        Resets episodic variables
        """
        self._episode_length = 0
        self._episode_reward = 0
        self._success = False
        if self._record_demo:
            self._demo.reset()

        peg_pos = np.hstack(
            [self.get_body_com("leg_bottom"), self.get_body_com("leg_top")]
        )

        if self._task == "remove":
            self._start_pos = np.array(
                [0.10600084, 0.15715909, 0.1496843, 0.24442536, -0.09417238, 0.23726938]
            )
            dist_to_start = np.linalg.norm(self._start_pos - peg_pos)
            self._prev_dist = dist_to_start
        elif self._task == "insert":
            self._goal_pos = np.array([0.0, 0.3, -0.5, 0.0, 0.3, -0.2])
            dist_to_goal = np.linalg.norm(self._goal_pos - peg_pos)
            self._prev_dist = dist_to_goal

    def reset_model(self):
        if self._task == "insert":
            # Reset peg above hole:
            qpos = np.array(
                [
                    0.44542705,
                    0.64189252,
                    -0.39544481,
                    -2.32144865,
                    -0.17935136,
                    -0.60320289,
                    1.57110214,
                ]
            )
        else:
            # Reset peg in hole
            qpos = np.array(
                [
                    0.52601062,
                    0.57254126,
                    -2.0747581,
                    -1.55342248,
                    0.15375072,
                    -0.5747922,
                    0.70163815,
                ]
            )
        qvel = np.zeros(7)
        self.set_state(qpos, qvel)
        return self._get_obs()

    def _remove_reward(self, s, a) -> Tuple[float, dict]:
        """Compute the peg removal reward.
        Note: We assume that the reward is computed on-policy, so the given
        state is equal to the current observation.
        Returns reward and info dict
        """
        info = {}
        peg_pos = np.hstack(
            [self.get_body_com("leg_bottom"), self.get_body_com("leg_top")]
        )
        dist_to_start = np.linalg.norm(self._start_pos - peg_pos)
        # we want the current distance to be smaller than the previous step's distnace
        dist_diff = self._prev_dist - dist_to_start
        self._prev_dist = dist_to_start
        peg_to_start_reward = dist_diff * self._peg_to_point_rew_coeff

        control_reward = np.dot(a, a) * self._control_penalty_coeff * -1
        peg_at_start = dist_to_start < self._goal_pos_threshold

        self._success = peg_at_start
        success_reward = 0
        if self._success:
            success_reward = self._success_rew

        if self._sparse:
            remove_reward = control_reward + success_reward
        else:
            remove_reward = peg_to_start_reward + control_reward + success_reward

        info["dist_to_start"] = dist_to_start
        info["control_rew"] = control_reward
        info["peg_to_start_rew"] = peg_to_start_reward
        info["success_rew"] = success_reward
        return remove_reward, info

    def _insert_reward(self, s, a) -> Tuple[float, dict]:
        """Compute the insertion reward.
        Note: We assume that the reward is computed on-policy, so the given
        state is equal to the current observation.
        """
        info = {}
        peg_pos = np.hstack(
            [self.get_body_com("leg_bottom"), self.get_body_com("leg_top")]
        )
        dist_to_goal = np.linalg.norm(self._goal_pos - peg_pos)
        dist_diff = self._prev_dist - dist_to_goal
        self._prev_dist = dist_to_goal
        peg_to_goal_reward = dist_diff * self._peg_to_point_rew_coeff

        control_reward = np.dot(a, a) * self._control_penalty_coeff * -1
        peg_at_goal = (
            dist_to_goal < self._goal_pos_threshold
            and self.get_body_com("leg_bottom")[2] < -0.45
        )

        self._success = peg_at_goal
        success_reward = 0
        if self._success:
            success_reward = self._success_rew

        if self._sparse:
            insert_reward = control_reward + success_reward
        else:
            insert_reward = peg_to_goal_reward + control_reward + success_reward

        info["dist_to_goal"] = dist_to_goal
        info["control_rew"] = control_reward
        info["peg_to_goal_rew"] = peg_to_goal_reward
        info["success_rew"] = success_reward
        return insert_reward, info

    def _get_obs(self) -> dict:
        """
        Returns the robot actuator states, and the object pose.
        By default, returns the object pose.
        """
        obs = {
            "object_ob": np.concatenate(
                [self.data.get_body_xpos("ball"), self.data.get_body_xquat("ball")]
            )
        }
        if self._robot_ob:
            obs["robot_ob"] = np.concatenate(
                [self.sim.data.qpos.flat, self.sim.data.qvel.flat]
            )
        return obs

    def render(self, mode="human"):
        img = super().render(mode, camera_id=0)
        if mode != "rgb_array":
            return img
        img = np.expand_dims(img, axis=0)
        img = img / 255.0
        return img

    def save_demo(self):
        self._demo.save(self.name)

    def load_demo(self, seed):
        pass

    def get_goal(self, ob):
        pass

    def is_success(self, ob, goal):
        pass

    def is_possible_goal(self, goal):
        return True

    def get_env_success(self, ob, goal):
        return self.is_success(ob, goal)

    def compute_reward(self, achieved_goal, goal, info=None):
        pass

    @property
    def dof(self) -> int:
        """
        Returns the DoF of the robot.
        """
        return 7

    @property
    def observation_space(self) -> dict:
        """
        Object ob: 7D pose of peg
        Robot ob: 14D qpos and qvel of robot 
        """
        ob_space = {"robot_ob": [14], "object_ob": [7]}

        return ob_space

    @property
    def action_space(self):
        """
        Returns ActionSpec of action space, see
        action_spec.py for more documentation.
        """
        return ActionSpec(self.dof)

    @property
    def goal_space(self):
        if self._goal_type == "state_obj":
            return [7]  # peg pose
        elif self._goal_type == "state_obj_robot":
            return [21]  # peg pose and robot qpos and robot qvel


if __name__ == "__main__":
    import time
    from config import create_parser

    parser = create_parser("PegInsertionEnv")
    parser.set_defaults(env="PegInsertionEnv")
    config, unparsed = parser.parse_known_args()
    env = PegInsertionEnv(config)
    env.reset()
    for _ in range(10000):
        # action = np.zeros_like(env.action_space.sample())
        # env.step(action)
        env.render()
        time.sleep(0.01)