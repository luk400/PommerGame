import pommerman
import pommerman.characters
import pommerman.envs
import numpy as np
import gym
import random

DEFAULT_FEATURE_CONFIG = {
    'recode_agents': True,
    'compact_powerups': True,
    'compact_structure': True,
    'rescale': True,
}


def make_np_float(feature):
    return np.array(feature).astype(np.float32)


def _rescale(x):
    return (x - 0.5) * 2.0


def get_feature_channels(config):
    num_channels = 15
    if config['recode_agents']:
        num_channels -= 2
    if config['compact_powerups']:
        num_channels -= 2
    if config['compact_structure']:
        num_channels -= 2
    return num_channels


def get_unflat_obs_space(channels=15, board_size=11, rescale=True):
    if len(channels):
        min_board_obs = np.zeros((channels[0], channels[1], channels[2]))
        max_board_obs = np.ones_like(min_board_obs)
        min_other_obs = np.zeros(0)
        max_other_obs = np.ones_like(min_other_obs)
    else:
        min_board_obs = np.zeros((channels, board_size, board_size))
        max_board_obs = np.ones_like(min_board_obs)
        min_other_obs = np.zeros(3)
        max_other_obs = np.ones_like(min_other_obs)

    if rescale:
        min_board_obs = _rescale(min_board_obs)
        max_board_obs = _rescale(max_board_obs)
        min_other_obs = _rescale(min_other_obs)
        max_other_obs = _rescale(max_other_obs)

    return gym.spaces.Tuple([
        gym.spaces.Box(min_board_obs, max_board_obs),
        gym.spaces.Box(min_other_obs, max_other_obs)])


def featurize(obs, agent_id, config):
    max_item = pommerman.constants.Item.Agent3.value

    ob = obs["board"]
    ob_bomb_blast_strength = obs["bomb_blast_strength"].astype(np.float32) / pommerman.constants.AGENT_VIEW_SIZE
    ob_bomb_life = obs["bomb_life"].astype(np.float32) / pommerman.constants.DEFAULT_BOMB_LIFE

    # one hot encode the board items
    ob_values = max_item + 1
    ob_hot = np.eye(ob_values)[ob]

    # replace agent item channels with friend, enemy, self channels
    if config['recode_agents']:
        self_value = pommerman.constants.Item.Agent0.value + agent_id
        enemies = np.logical_and(ob >= pommerman.constants.Item.Agent0.value, ob != self_value)
        self = (ob == self_value)
        friends = (ob == pommerman.constants.Item.AgentDummy.value)
        ob_hot[:, :, 9] = friends.astype(np.float32)
        ob_hot[:, :, 10] = self.astype(np.float32)
        ob_hot[:, :, 11] = enemies.astype(np.float32)
        ob_hot = np.delete(ob_hot, np.s_[12::], axis=2)

    if config['compact_powerups']:
        # replace powerups with single channel
        powerup = ob_hot[:, :, 6] * 0.5 + ob_hot[:, :, 7] * 0.66667 + ob_hot[:, :, 8]
        ob_hot[:, :, 6] = powerup
        ob_hot = np.delete(ob_hot, [7, 8], axis=2)

    # replace bomb item channel with bomb life
    ob_hot[:, :, 3] = ob_bomb_life

    if config['compact_structure']:
        ob_hot[:, :, 0] = 0.5 * ob_hot[:, :, 0] + ob_hot[:, :, 5]  # passage + fog
        ob_hot[:, :, 1] = 0.5 * ob_hot[:, :, 2] + ob_hot[:, :, 1]  # rigid + wood walls
        ob_hot = np.delete(ob_hot, [2], axis=2)
        # replace former fog channel with bomb blast strength
        ob_hot[:, :, 5] = ob_bomb_blast_strength
    else:
        # insert bomb blast strength next to bomb life
        ob_hot = np.insert(ob_hot, 4, ob_bomb_blast_strength, axis=2)

    self_ammo = make_np_float([obs["ammo"]])
    self_blast_strength = make_np_float([obs["blast_strength"]])
    self_can_kick = make_np_float([obs["can_kick"]])

    ob_hot = ob_hot.transpose((2, 0, 1))  # PyTorch tensor layout compat

    if config['rescale']:
        ob_hot = _rescale(ob_hot)
        self_ammo = _rescale(self_ammo / 10)
        self_blast_strength = _rescale(self_blast_strength / pommerman.constants.AGENT_VIEW_SIZE)
        self_can_kick = _rescale(self_can_kick)

    return np.concatenate([
        np.reshape(ob_hot, -1), self_ammo, self_blast_strength, self_can_kick])


class PommermanEnvWrapper(gym.Wrapper):
    def __init__(self, env=None, feature_config=DEFAULT_FEATURE_CONFIG, obs_shape=None):
        super(PommermanEnvWrapper, self).__init__(env)
        self.feature_config = feature_config
        if feature_config and len(feature_config):
            self._set_observation_space(channels=get_feature_channels(feature_config))
        else:
            self._set_observation_space(obs_shape)

    def _set_observation_space(self, channels):
        # The observation space cannot contain multiple tensors due to compat issues
        # with the way storages, normalizers, etc work. Thus, easiest to flatten everything and restore
        # shape in NN model
        bs = self.env._board_size
        obs_unflat = get_unflat_obs_space(channels, bs, self.feature_config['rescale'] if self.feature_config else True)
        min_flat_obs = np.concatenate([obs_unflat.spaces[0].low.flatten(), obs_unflat.spaces[1].low])
        max_flat_obs = np.concatenate([obs_unflat.spaces[0].high.flatten(), obs_unflat.spaces[1].high])

        self.observation_space = gym.spaces.Box(min_flat_obs, max_flat_obs)

    def seed(self, seed=None):
        if seed is not None:
            np.random.seed(seed)
            random.seed(seed)

    def step(self, actions):
        ret, opp_ret = self.env.step(actions)
        state, reward, done, _ = ret
        if self.feature_config:
            agent_state = featurize(
                state,
                self.env.training_agent,
                self.feature_config)
        else:
            agent_state = np.array(state).flatten()

        return agent_state, reward, done, {}

    def reset(self):
        obs = self.env.reset()
        if self.feature_config:
            agent_obs = featurize(
                obs[self.env.training_agent],
                self.env.training_agent,
                self.feature_config)
        else:
            agent_obs = np.array(obs[self.env.training_agent]).flatten()
        return agent_obs


class TrainingAgent(pommerman.agents.BaseAgent):

    def __init__(self, character=pommerman.characters.Bomber):
        super(TrainingAgent, self).__init__(character)

    def act(self, obs, action_space):
        """This agent has its own way of inducing actions."""
        return None


def make_env(config):
    training_agent = TrainingAgent()
    agent_list = [
        training_agent,
        pommerman.agents.SimpleAgent(),
        pommerman.agents.SimpleAgent(),
        pommerman.agents.SimpleAgent(),
    ]
    if config == "GraphicOVOCompact-v0":
        env = pommerman.make(config, agent_list[:2])
    else:
        env = pommerman.make(config, agent_list)
    env.set_training_agent(training_agent.agent_id)
    env.set_init_game_state(None)
    return PommermanEnvWrapper(env)
