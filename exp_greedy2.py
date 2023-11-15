# -*- coding: utf-8 -*-
"""SSD_PyTorch

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1w4Svq_UdSomkR6ugSd_02K17K5_Yczb2

## Setup
"""
from __future__ import annotations
import gymnasium as gym
import math
import random
import matplotlib
import matplotlib.pyplot as plt
from collections import namedtuple, deque
from itertools import count
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from tqdm import tqdm
from collections import defaultdict

from gymnasium.spaces import Box, Dict, Discrete, MultiDiscrete, Tuple
import seaborn as sns
from matplotlib.colors import ListedColormap
import matplotlib.pyplot as plt
from matplotlib import colors
from ray.rllib.env import MultiAgentEnv

from agents.cleanup_agent import CleanupAgent, GreedyCleanUpAgent

# set up matplotlib
is_ipython = 'inline' in matplotlib.get_backend()
if is_ipython:
    from IPython import display

plt.ion()

# if GPU is to be used
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"cuda available: {torch.cuda.is_available()}")
np.set_printoptions(threshold=np.inf)


num_agents = 10
reward_multiplier = 43
pp = False
verbose = False
verbose_episode = 1999


"""## Q-Network"""


def preprocess_inputs(env_states):
    # Convert agent positions to one-hot grid maps
    agent_maps = None
    a_keys = sorted(env_states.keys())  # key must be sorted
    for k in a_keys:
        if agent_maps is None:
            agent_maps = env_states[k]
        else:
            agent_maps = np.concatenate((agent_maps, env_states[k]), axis=0)
    # Stack all maps along the channel dimension
    return agent_maps


class UNetwork(nn.Module):
    # number of apples that a given situation will generate later
    def __init__(self, num_agents):
        super(UNetwork, self).__init__()
        self.pref_embedding = 1
        self.coord1 = nn.Linear(2, 64)
        self.coord2 = nn.Linear(64, 32)
        self.coord3 = nn.Linear(32, self.pref_embedding)
        torch.nn.init.xavier_uniform_(self.coord1.weight)
        torch.nn.init.xavier_uniform_(self.coord2.weight)
        torch.nn.init.xavier_uniform_(self.coord3.weight)
        # self.rnn = nn.RNN(input_size=2, hidden_size=32, num_layers=2, batch_first=True)

    def forward(self, coord):
        c = coord.view(coord.size(0), -1)
        # c, _ = self.rnn(c)
        c = self.coord1(c)
        c = torch.relu(self.coord2(c))
        c = self.coord3(c)
        c = c.view(coord.size(0), self.pref_embedding)
        return c


GAMMA_U = 0.99


class ReplayBuffer:
    def __init__(self, buffer_size_u):
        self.buffer_size_u = buffer_size_u
        self.buffer_u = deque(maxlen=buffer_size_u)

    def add_u(self, experience):
        self.buffer_u.append(experience)

    def sample_u(self, batch_size):
        return random.sample(self.buffer_u, batch_size)
        # return list(self.buffer)  # most recent, for almost-online learning

    def __len__(self):
        return len(self.buffer_u)


class CentralizedAgent:
    def __init__(self, num_agents, action_size, buffer_size_u=8000,
                 batch_size=32):
        self.num_agents = num_agents
        # self.input_shape = input_shape
        self.action_size = action_size
        self.batch_size = batch_size

        self.u_network = UNetwork(num_agents).to(device)
        self.u_optimizer = torch.optim.Adam(self.u_network.parameters(), lr=0.0001)
        self.memory = ReplayBuffer(buffer_size_u)

    def step(self, step_apple_reward, info_vec, new_info_vec):
        # Save experience in replay memory
        self.memory.add_u((step_apple_reward, info_vec, new_info_vec))

        if len(self.memory) >= self.batch_size:
            experiences_u = self.memory.sample_u(self.batch_size)
            self.train(experiences_u)

    def get_actions(self, states):
        states = torch.from_numpy(states).float().unsqueeze(0).to(device)
        # info_vec = torch.from_numpy(info_vec).float().unsqueeze(0).to(device)

    def train(self, experiences_u):
        step_apple_reward, info_vec, new_info_vec = zip(*experiences_u)
        step_apple_reward = torch.from_numpy(np.vstack(step_apple_reward)).float().to(device)
        info_vec = torch.from_numpy(np.stack(info_vec)).float().to(device)
        new_info_vec = torch.from_numpy(np.stack(new_info_vec)).float().to(device)


        # Update U function
        u_values = self.u_network(info_vec)
        next_u_values = self.u_network(new_info_vec)

        u_targets = step_apple_reward + GAMMA_U * next_u_values

        # print(info_vec)
        # print(u_values)
        # print(step_apple_reward)

        u_loss = F.mse_loss(u_values, u_targets)
        self.u_optimizer.zero_grad()
        u_loss.backward()
        self.u_optimizer.step()


thresholdDepletion = 0.4
thresholdRestoration = 0.0
wasteSpawnProbability = 0.5
appleRespawnProbability = 0.05
dirt_multiplier = 10

class CleanupEnv(MultiAgentEnv):
    """
    Cleanup environment. In this game, the agents must clean up the dirt from the river before apples can spawn.
    Agent reward is only given for eating apples, meaning the agents must learn to clean up the dirt first and
    must learn to balance their individual rewards with the collective goal of cleaning up the river.
    """

    def __init__(self, num_agents=5, height=25, width=18, greedy=False):
        """
        Initialise the environment.
        """
        self.num_agents = num_agents
        self.timestamp = 0

        self.greedy = greedy
        self.height = height
        self.width = width
        self.dirt_end = round((1 / 3) * self.width)
        self.potential_waste_area = self.dirt_end * self.height
        self.apple_start = round((2 / 3) * self.width)

        self.action_space = Discrete(4)  # directional movement
        self.observation_space = Box(low=-1, high=151, shape=(1, 10), dtype=np.int32)
        self.agents = {}

        self.num_dirt = 0
        self.num_apples = 0
        self.current_apple_spawn_prob = appleRespawnProbability
        self.current_waste_spawn_prob = wasteSpawnProbability
        self.map = np.zeros((self.height, self.width))
        for i in range(0, self.height, 2):
            for j in range(self.dirt_end):
                self.map[i][j] = -1
                self.num_dirt += 1
        self.compute_probabilities()
        self._agent_ids = self.setup_agents()

        self.apple_reward = 1
        self.dirt_reward = 1
        self.total_apple_consumed = 0
        self.step_apple_consumed = 0
        self.epsilon = 1.0
        self.epsilon_decay = 0.9999

        super().__init__()

    def setup_agents(self):
        agent_ids = set()
        for i in range(self.num_agents):
            agent_id = str(i)
            spawn_point = [random.randint(0, self.height - 1), random.randint(0, self.width - 1)]
            while spawn_point[0] % 2 == 0 and spawn_point[1] < self.dirt_end:
                # do not spawn on dirt
                spawn_point = [random.randint(0, self.height - 1), random.randint(0, self.width - 1)]
            if not self.greedy:
                agent = CleanupAgent(agent_id, spawn_point)
            else:
                agent = GreedyCleanUpAgent(agent_id, spawn_point, -1)
            self.agents[agent_id] = agent
            agent_ids.add(agent_id)
        return agent_ids

    def reset(self, seed: int | None = None, options: dict = None) -> tuple:
        """
        Reset the environment.
        """
        options = options if options is not None else dict()
        # Set seed
        super().reset(seed=seed)
        self.timestamp = 0
        self.agents = {}
        self.num_dirt = 0
        self.num_apples = 0
        self.map = np.zeros((self.height, self.width))
        self.current_apple_spawn_prob = appleRespawnProbability
        self.current_waste_spawn_prob = wasteSpawnProbability
        for i in range(0, self.height, 2):
            for j in range(self.dirt_end):
                self.map[i][j] = -1
                self.num_dirt += 1
        self.compute_probabilities()
        self.setup_agents()

        self.total_apple_consumed = 0
        self.step_apple_consumed = 0

        observations = {}
        inf = np.array([self.num_apples, self.num_dirt])
        u_input0 = torch.tensor(inf).float().unsqueeze(0).to(device)
        u_t = centralAgent.u_network(u_input0)
        inf[1] -= 1
        u_input1 = torch.tensor(inf).float().unsqueeze(0).to(device)
        u_tp = centralAgent.u_network(u_input1)
        self.dirt_reward = (u_tp - u_t).item() * dirt_multiplier
        print(f"(u_tp: {u_tp.item()}) - (u_t: {u_t.item()}) = dirt reward: {self.dirt_reward}")

        return observations, self.generate_info()

    def step(self):
        """
        Take a step in the environment.
        """
        observations = {}
        rewards = {}
        dones = {}
        has_agent = set()
        self.timestamp += 1
        self.step_apple_consumed = 0
        train_state = []
        train_new_state = []
        train_reward = []

        for agent in [self.agents[key] for key in sorted(self.agents)]:

            inf = np.array([self.num_apples, self.num_dirt])
            # train_state.append(inf)
            u_input0 = torch.tensor(inf).float().unsqueeze(0).to(device)
            u_t = centralAgent.u_network(u_input0)
            inf[1] -= 1
            u_input1 = torch.tensor(inf).float().unsqueeze(0).to(device)
            u_tp = centralAgent.u_network(u_input1)
            self.dirt_reward = (u_tp - u_t).item() * dirt_multiplier
            gi = self.generate_info()
            if gi["cleaner"] == 0:
                dr = self.dirt_reward
            else:
                dr = self.dirt_reward / gi["cleaner"]
            if gi["picker"] == 0:
                ar = self.apple_reward
            else:
                ar = self.apple_reward / gi["picker"]

            if random.random() > max(self.epsilon, 0.05):
                if dr > ar:
                    agent.region = -1
                else:
                    agent.region = 1
            else:
                choice = np.random.choice(2)
                if choice == 0:
                    agent.region = 1
                else:
                    agent.region = -1

            action = self.get_greedy_action(agent)
            reward = 0
            if action == 0:  # up
                x, new_y = agent.pos[0], agent.pos[1]  # y is not exactly new
                new_x = x - 1 if x > 0 else x
                if (new_x, new_y) not in has_agent:
                    agent.pos = np.array([new_x, new_y])
                else:
                    new_x = x
                has_agent.add((new_x, new_y))
                reward += self.calculate_reward(new_x, new_y)
            elif action == 1:  # right
                new_x, y = agent.pos[0], agent.pos[1]
                new_y = y + 1 if y < self.width - 1 else y
                if (new_x, new_y) not in has_agent:
                    agent.pos = np.array([new_x, new_y])
                else:
                    new_y = y
                has_agent.add((new_x, new_y))
                reward += self.calculate_reward(new_x, new_y)
            elif action == 2:  # down
                x, new_y = agent.pos[0], agent.pos[1]
                new_x = x + 1 if x < self.height - 1 else x
                if (new_x, new_y) not in has_agent:
                    agent.pos = np.array([new_x, new_y])
                else:
                    new_x = x
                has_agent.add((new_x, new_y))
                reward += self.calculate_reward(new_x, new_y)
            elif action == 3:  # left
                new_x, y = agent.pos[0], agent.pos[1]
                new_y = y - 1 if y > 0 else y
                if (new_x, new_y) not in has_agent:
                    agent.pos = np.array([new_x, new_y])
                else:
                    new_y = y
                has_agent.add((new_x, new_y))
                reward += self.calculate_reward(new_x, new_y)
            rewards[agent.agent_id] = reward
            agent.reward += reward
            # inf2 = np.array([self.num_apples, self.num_dirt])
            # train_new_state.append(inf2)
            # train_reward.append(reward)
        self.compute_probabilities()
        self.spawn_apples_and_waste(has_agent)
        self.epsilon = self.epsilon * self.epsilon_decay
        # u_input0 = torch.tensor([self.num_apples, self.num_dirt]).float().unsqueeze(0).to(device)
        # u_t = centralAgent.u_network(u_input0)
        # u_input1 = torch.tensor([self.num_apples, self.num_dirt-1]).float().unsqueeze(0).to(device)
        # u_tp = centralAgent.u_network(u_input1)
        # self.dirt_reward = u_tp - u_t

        gi = self.generate_info()
        inf = np.array([self.num_apples, self.num_dirt])
        # p = self.generate_info()["pos"].flatten()
        # inf = np.concatenate((inf, p))
        u_input0 = torch.tensor(inf).float().unsqueeze(0).to(device)
        u_t = centralAgent.u_network(u_input0)
        inf[1] -= 1
        u_input1 = torch.tensor(inf).float().unsqueeze(0).to(device)
        u_tp = centralAgent.u_network(u_input1)
        self.dirt_reward = (u_tp - u_t).item() * dirt_multiplier
        if verbose:
            print(f"num apple: {self.num_apples}, num dirt: {self.num_dirt}")
            print(f"rew apple: {self.apple_reward}, rew dirt: {self.dirt_reward}")
            print(f"(u_tp: {u_tp.item()}) - (u_t: {u_t.item()}) = dirt reward: {self.dirt_reward}")

        rewards["apple"] = self.total_apple_consumed
        rewards["step_apple"] = self.step_apple_consumed
        dones["__all__"] = self.timestamp == 1000
        return observations, rewards, dones, {"__all__": False}, self.generate_info(), train_state, train_new_state, train_reward

    def greedily_move_to_closest_object(self):
        """
        Each agent moves to the closest object
        """
        # assert (self.greedy)
        actions = {}
        for agent in self.agents.values():
            actions[agent.agent_id] = self.get_greedy_action(agent)
        return actions

    def generate_info(self):
        info = {"apple": self.num_apples, "dirt": self.num_dirt, "x1": 0,
                "x2": 0, "x3": 0, "picker": 0, "cleaner": 0}
        for aid, agent in self.agents.items():
            y = agent.pos[1]
            if y < 6:
                info["x1"] += 1
            elif y >= 12:
                info["x3"] += 1
            else:
                info["x2"] += 1
            if agent.region == 1:
                info["picker"] += 1
            else:
                info["cleaner"] += 1

        keys = sorted(self.agents.keys())
        pos = np.zeros((self.num_agents, 2))
        for agent_key in keys:
            pos[int(agent_key)] = self.agents[agent_key].pos
        info["pos"] = pos
        return info

    def calculate_reward(self, x, y):
        if self.map[x][y] == -1:
            self.map[x][y] = 0
            self.num_dirt -= 1
            return self.dirt_reward
        if self.map[x][y] == 1:
            self.map[x][y] = 0
            self.num_apples -= 1
            self.total_apple_consumed += 1
            self.step_apple_consumed += self.apple_reward
            return self.apple_reward
        return 0

    def compute_probabilities(self):
        waste_density = 0
        if self.potential_waste_area > 0:
            waste_density = self.num_dirt / self.potential_waste_area
        if waste_density >= thresholdDepletion:
            self.current_apple_spawn_prob = 0
            self.current_waste_spawn_prob = 0
        else:
            self.current_waste_spawn_prob = wasteSpawnProbability
            if waste_density <= thresholdRestoration:
                self.current_apple_spawn_prob = appleRespawnProbability
            else:
                spawn_prob = (1 - (waste_density - thresholdRestoration)
                              / (thresholdDepletion - thresholdRestoration)) \
                             * appleRespawnProbability
                self.current_apple_spawn_prob = spawn_prob

    def spawn_apples_and_waste(self, has_agent):
        # spawn apples, multiple can spawn per step
        for i in range(self.height):
            for j in range(self.apple_start, self.width, 1):
                rand_num = np.random.rand(1)[0]
                if rand_num < self.current_apple_spawn_prob and (i, j) not in has_agent:
                    if (self.map[i][j] != 1):
                        self.num_apples += 1
                    self.map[i][j] = 1

        # spawn one waste point, only one can spawn per step
        if self.num_dirt < self.potential_waste_area:
            dirt_spawn = [random.randint(0, self.height - 1), random.randint(0, self.dirt_end)]
            while self.map[dirt_spawn[0]][dirt_spawn[1]] == -1:  # do not spawn on already existing dirt
                dirt_spawn = [random.randint(0, self.height - 1), random.randint(0, 5)]

            rand_num = np.random.rand(1)[0]
            if rand_num < self.current_waste_spawn_prob and (dirt_spawn[0], dirt_spawn[1]) not in has_agent:
                self.map[dirt_spawn[0]][dirt_spawn[1]] = -1
                self.num_dirt += 1

    def find_nearest_apple_from_agent(self, agent):
        # assert (self.greedy)
        x, y = agent.pos
        closest_x, closest_y, min_distance = -1, -1, float('inf')
        for i in range(self.height):
            for j in range(self.width):
                if self.map[i][j] == 1 and abs(i - x) + abs(j - y) <= min_distance:
                    min_distance = abs(i - x) + abs(j - y)
                    closest_x, closest_y = i, j
        return [closest_x, closest_y], min_distance

    def find_nearest_waste_from_agent(self, agent):
        # assert (self.greedy)
        x, y = agent.pos
        closest_x, closest_y, min_distance = -1, -1, float('inf')
        for i in range(self.height):
            for j in range(self.width):
                if self.map[i][j] == -1 and abs(i - x) + abs(j - y) <= min_distance:
                    min_distance = abs(i - x) + abs(j - y)
                    closest_x, closest_y = i, j
        return [closest_x, closest_y], min_distance

    def get_greedy_action(self, agent):
        # assert (self.greedy)
        if agent.region == 1:
            nearest_obj = self.find_nearest_apple_from_agent(agent)[0]
        else:
            nearest_obj = self.find_nearest_waste_from_agent(agent)[0]
        if agent.pos[0] == nearest_obj[0]:
            if nearest_obj[1] < agent.pos[1]:
                return 3
            return 1
        if agent.pos[0] > nearest_obj[0]:
            return 0
        return 2

    def render(self):
        """
        Render the environment.
        """
        cmap = colors.ListedColormap(['tab:brown', 'white', 'green'])
        bounds = [-1, -0.5, 0.5, 1]
        norm = colors.BoundaryNorm(bounds, cmap.N)
        # create discrete colormap
        plt.rcParams["figure.figsize"] = [10, 10]
        fig, ax = plt.subplots()

        for agent in self.agents.values():
            t = "{}({}) ".format(agent.agent_id, agent.reward)
            plt.text(agent.pos[1] - 0.4, agent.pos[0], t, fontsize=8)
        ax.imshow(self.map, cmap=cmap, norm=norm)
        # draw gridlines
        ax.grid(which='major', axis='both', linestyle='-', color='k', linewidth=1)
        ax.set_xticks(np.arange(-.5, self.width, 1))
        ax.set_yticks(np.arange(-.5, self.height, 1))
        # if not labels:
        plt.tick_params(bottom=False, top=False, left=False, right=False, labelbottom=False, labelleft=False)

        plt.show()


env = CleanupEnv(num_agents=num_agents, greedy=True)
num_epochs = 200
max_steps_per_epoch = 1000
input_shape = (num_agents, 2)
action_size = 2  # apple, dirt

reward_graph = []
# garbage_collected = []
weight_graph = defaultdict(list)
dirt_reward_graph = []

# Create environment and agent
centralAgent = CentralizedAgent(num_agents, action_size)

# Training loop
f = open("log_greedy2.txt", "w")
f_good = open("good_log_greedy2.txt", "w")
for epoch in range(num_epochs):
    print(f"=============== episode {epoch} ===============")
    f.write(f"=============== episode {epoch} ===============\n")
    # Reset environment and get initial state
    epoch_reward = 0
    env_states, info = env.reset()
    states = preprocess_inputs(env_states)
    info_vec = np.array([info["apple"], info["dirt"]])

    good_epoch_apple = []
    good_epoch_dirt = []
    good_epoch_x1 = []
    good_epoch_x2 = []
    good_epoch_x3 = []

    print(f"num apple: {env.num_apples}, num dirt: {env.num_dirt}")
    print(f"Starting rewards apple : {env.apple_reward}, dirt (should be big): {env.dirt_reward}")
    dirt_reward_graph.append(env.dirt_reward)
    print(dirt_reward_graph)
    if epoch > verbose_episode:
        verbose = True
    for step in tqdm(range(max_steps_per_epoch)):
        # Agent takes action
        # Environment responds
        next_env_states, env_rewards, dones, _, info, ts, tns, tr = env.step()
        if info["dirt"] == 0:
            good_epoch_apple.append(info["apple"])
        else:
            good_epoch_apple.append(info["apple"]/info["dirt"])
        good_epoch_dirt.append(info["dirt"])
        good_epoch_x1.append(info["x1"])
        good_epoch_x2.append(info["x2"])
        good_epoch_x3.append(info["x3"])

        new_info_vec = np.array([info["apple"], info["dirt"]])

        epoch_reward = env_rewards["apple"]
        step_apple_reward = env_rewards["step_apple"]
        # for i in range(num_agents):
        #     centralAgent.step(tr[i], ts[i], tns[i])

        centralAgent.step(step_apple_reward, info_vec, new_info_vec)

        # Update state
        info_vec = new_info_vec
        if verbose:
            print(f"{step}: num apple: {env.num_apples}, num dirt: {env.num_dirt}")
            print(f"{step}: rewards apple : {env.apple_reward}, dirt: {env.dirt_reward}")

        if dones["__all__"]:
            break
    # agent.scheduler.step()

    if epoch_reward > 2000:
        f_good.write(f"Epoch number: {epoch}\n")
        f_good.write(f"Epoch reward: {epoch_reward}\n")
        f_good.write(f"Epoch apple/dirt\n")
        f_good.write(f"{good_epoch_apple}\n\n")
        f_good.write(f"Epoch dirt\n")
        f_good.write(f"{good_epoch_dirt}\n\n")
        f_good.write(f"Epoch x1\n")
        f_good.write(f"{good_epoch_x1}\n\n")
        f_good.write(f"Epoch x2\n")
        f_good.write(f"{good_epoch_x2}\n\n")
        f_good.write(f"Epoch x3\n")
        f_good.write(f"{good_epoch_x3}\n\n")

    print(f"Epoch reward: {epoch_reward}")
    reward_graph.append(epoch_reward)
    print("Reward graph: ")
    print(reward_graph)
    f.write("Reward graph: \n")
    f.write(f"{reward_graph}\n")

    # garbage_collected.append(epoch_garbage)
    # print("garbage graph: ")
    # print(garbage_collected)
    # f.write("garbage graph: \n")
    # f.write(f"{garbage_collected}\n")

    # weight1_1 = centralAgent.q_network._modules['fc1'].weight[0][0]
    # weight1_2 = centralAgent.q_network._modules['fc1'].weight[1][1]
    # weight1_3 = centralAgent.q_network._modules['fc1'].weight[10][10]
    # weight1_4 = centralAgent.q_network._modules['fc1'].weight[12][2]
    # weight2_1 = centralAgent.q_network._modules['fc2'].weight[0][0]
    # weight2_2 = centralAgent.q_network._modules['fc2'].weight[3][3]
    # weight2_3 = centralAgent.q_network._modules['fc2'].weight[14][12]
    # weight3_1 = centralAgent.q_network._modules['fc3'].weight[0][0]
    # weight3_2 = centralAgent.q_network._modules['fc3'].weight[5][5]
    # weight3_3 = centralAgent.q_network._modules['fc3'].weight[14][12]
    # weight_graph[11].append(weight1_1.item())
    # weight_graph[12].append(weight1_2.item())
    # weight_graph[13].append(weight1_3.item())
    # weight_graph[14].append(weight1_4.item())
    # weight_graph[21].append(weight2_1.item())
    # weight_graph[22].append(weight2_2.item())
    # weight_graph[23].append(weight2_3.item())
    # weight_graph[31].append(weight3_1.item())
    # weight_graph[32].append(weight3_2.item())
    # weight_graph[33].append(weight3_3.item())

    print(weight_graph[13])

    print(f"Ending num apple: {env.num_apples}, num dirt: {env.num_dirt}")
    print(f"Ending rewards apple : {env.apple_reward}, dirt(should be small): {env.dirt_reward}")

    print(f"Epoch reward: {epoch_reward}")
    if (epoch + 1) % 100 == 0:
        print(f"Epoch {epoch} completed")
        f.write("Weight Graph: \n")
        # f.write(f"{weight_graph[11]}\n\n")
        # f.write(f"{weight_graph[12]}\n\n")
        # f.write(f"{weight_graph[13]}\n\n")
        # f.write(f"{weight_graph[14]}\n\n")
        # f.write(f"{weight_graph[21]}\n\n")
        # f.write(f"{weight_graph[22]}\n\n")
        # f.write(f"{weight_graph[23]}\n\n")
        # f.write(f"{weight_graph[31]}\n\n")
        # f.write(f"{weight_graph[32]}\n\n")
        # f.write(f"{weight_graph[33]}\n\n")
f.close()
f_good.close()
