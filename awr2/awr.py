# parallel awr.. don't know if it will work fine
import torch
import tqdm
import os
from torch import nn
import copy
import numpy as np
from models import actor, critic
from normalizer import Normalizer
from collections import deque
import multiprocessing

# get the flat grads or params
def _get_flat_params_or_grads(network, mode='params'):
    """
    include two kinds: grads and params
    """
    attr = 'data' if mode == 'params' else 'grad'
    out = [] 
    for param in network.parameters():
        xx = getattr(param, attr) 
        if xx is not None: 
            out.append(xx.reshape(-1).cpu())
    return torch.cat(out)


def _set_flat_params_or_grads(network, flat_params, mode='params'):
    """
    include two kinds: grads and params
    """
    attr = 'data' if mode == 'params' else 'grad'
    # the pointer
    pointer = 0
    for param in network.parameters():
        #getattr(param, attr).copy_(torch.tensor(flat_params[pointer:pointer + param.data.numel()]).view_as(param.data))
        xx = getattr(param, attr)
        if xx is None: continue
        xx.copy_(flat_params[pointer:pointer + param.data.numel()].view_as(param.data))
        pointer += param.data.numel()


class AsyncDDPGAgent:
    def __init__(self, observation_space, action_space,
                 discount=0.99, td_lambda=0.95, hidden_size=(128, 64),
                 temp=1., max_weight=20, action_std=0.4,
                 actor_lr=0.0001, critic_lr=0.01, device='cpu',
                 batch_size=256, pipe=None, optimizer='SGD', activation='relu'):

        self.device = device
        inp_dim = observation_space.shape[0]
        self.actor = actor(inp_dim, action_space.low.shape[0], std=action_std, hidden_size=hidden_size, activation=activation).to(device)
        self.critic = critic(inp_dim, hidden_size=hidden_size, activation=activation).to(device)
        self.normalizer = Normalizer((inp_dim,), default_clip_range=5).to(device)
        self.normalizer.count += 1 #unbiased ...
        self.temp = temp
        self.max_weight = max_weight

        # NOTE: optimizer is different
        if optimizer == 'SGD':
            self.optim_actor = torch.optim.SGD(self.actor.parameters(), actor_lr, momentum=0.9)
            self.optim_critic = torch.optim.SGD(self.critic.parameters(), critic_lr, momentum=0.9)
        else:
            self.optim_actor = torch.optim.Adam(self.actor.parameters(), actor_lr)
            self.optim_critic = torch.optim.Adam(self.critic.parameters(), critic_lr)
        self.pipe = pipe
        self.batch_size = batch_size
        self.mse = nn.MSELoss()

        self.discount = discount
        self.td_lambda = td_lambda
        self.val_norm = 1.0 / (1.0 - self.discount)

        self.action_mean = ((action_space.high + action_space.low)/2)[None,:]
        self.action_std  = ((action_space.high - action_space.low)/2)[None,:]

    def set_params(self, params):
        assert isinstance(params, dict)
        _set_flat_params_or_grads(self.actor, params['actor'], mode='params'),
        _set_flat_params_or_grads(self.critic, params['critic'], mode='params')

    def get_params(self):
        return {
            'actor': _get_flat_params_or_grads(self.actor, mode='params'),
            'critic': _get_flat_params_or_grads(self.critic, mode='params')
        }


    def sync_grads(self, net, weight=None):
        # reweight the gradients to avoid any bias...
        grad = _get_flat_params_or_grads(net, mode='grad')
        if weight is not None:
            grad = grad * weight
        self.pipe.send(grad)
        grad = self.pipe.recv()
        _set_flat_params_or_grads(net, grad, mode='grad')


    def update_normalizer(self, obs):
        data= [obs.sum(axis=0), (obs**2).sum(axis=0), obs.shape[0]]
        self.pipe.send(data)
        s, sq, count = self.pipe.recv()
        self.normalizer.add(
            torch.tensor(s, dtype=torch.float32, device=self.device),
            torch.tensor(sq, dtype=torch.float32, device=self.device),
            torch.tensor(count, dtype=torch.long, device=self.device),
        )

    def tensor(self, x):
        return torch.tensor(x, dtype=torch.float).to(self.device)

    def as_state(self, s):
        return self.normalizer(self.tensor(s))

    def gen(self, sample_idx, steps, batch_size):
        while steps > 0:
            np.random.shuffle(sample_idx)
            for i in range(len(sample_idx)//batch_size):
                if steps <= 0:
                    break
                yield sample_idx[i*batch_size:(i+1)*batch_size]
                steps -= 1

    def update_actor(self, steps, states, normed_actions, normed_advs, sample_idx, process_weight=None):
        # we assume all numpy states is not normalized ...
        #NOTE: it's better to have uniform sampling
        num_idx = len(states)
        for idx in self.gen(sample_idx, steps, self.batch_size):
            self.optim_actor.zero_grad()
            weights = np.minimum(np.exp(normed_advs[idx]/self.temp), self.max_weight)
            weights = self.tensor(weights)
            distribution = self.actor(self.as_state(states[idx]))
            logpi = - distribution.log_prob(self.tensor(normed_actions[idx])).sum(dim=-1)
            assert logpi.shape == weights.shape, f"logpi size: {logpi.shape}, weights size: {weights.shape}"
            actor_loss = (logpi * weights).mean()
            actor_loss.backward()

            self.sync_grads(self.actor, process_weight)
            self.optim_actor.step()

    def update_critic(self, steps, states, targets, sample_idx, process_weight=None):
        num_idx = len(states)
        for idx in self.gen(sample_idx, steps, self.batch_size):
            self.optim_critic.zero_grad()

            normed_val = self.critic(self.as_state(states[idx]))
            normed_target = self.tensor(targets[idx]) / self.val_norm
            assert normed_val.shape == normed_target.shape
            critic_loss = self.mse(normed_val, normed_target)
            critic_loss.backward()

            self.sync_grads(self.critic, process_weight)
            self.optim_critic.step()

    def tocpu(self, x):
        return x.detach().cpu().numpy()

    def act(self, obs, mode='sample'):
        obs = self.as_state(obs)
        p = self.actor(obs)
        if mode == 'sample':
            a = p.sample()
        else:
            a = p.loc
        return self.tocpu(a) * self.action_std + self.action_mean

    def value(self, obs):
        # the value networks' output is unnormalized term ..
        return self.tocpu(self.critic(self.as_state(obs)) * self.val_norm)

    def update(self, buffer, critic_steps, actor_steps, ADV_EPS=1e-5, process_weight=None):
        states, actions, rewards, dones = [np.array(i) for i in buffer]
        # normalize action
        actions = (actions - self.action_mean)/self.action_std

        dones = dones.astype(np.bool)

        valid_mask = ~dones
        sample_idx = np.arange(len(states))[valid_mask]

        values = self.value(states)
        discount_return = self.discount_return(rewards, dones, values, self.discount, self.td_lambda)
        #print('critic', critic_steps, end='\n\n')

        self.update_critic(critic_steps, states, discount_return, sample_idx, process_weight=process_weight)
        #print('finish critic', end='\n\n')

        # normalize advantages..
        # we need to compute the value again
        values = self.value(states)
        discount_return = self.discount_return(rewards, dones, values, self.discount, self.td_lambda)
        adv = discount_return - values
        adv_valid = adv[valid_mask]
        adv_norm = (adv - adv_valid.mean())/(adv_valid.std() + ADV_EPS)

        #print('actor', actor_steps)
        self.update_actor(actor_steps, states, actions, adv_norm, sample_idx, process_weight=process_weight)
        #print('finish actor')


    def discount_return(self, reward, done, value, discount, td_lambda):
        num_step = len(value)
        return_t = np.zeros([num_step])
        nxt = 0
        for t in range(num_step-1, -1, -1):
            if done[t]:
                #nxt = value[t]
                nxt = 0
            else:
                nxt_return = reward[t] + discount * nxt
                return_t[t] = nxt_return
                nxt = (1.0 - td_lambda) * value[t] + td_lambda * nxt_return
        return return_t

    def save(self, path):
        pipe = self.pipe
        self.pipe = None
        torch.save(self, path)
        self.pipe = pipe


class Worker(multiprocessing.Process):
    START = 1
    EXIT = 2
    ASK = 3
    GET_PARAM = 4
    SET_PARAM = 5
    COPY = 6
    SET_ENV = 7
    RESET = 8

    def __init__(self, make, env_name,
                 replay_buffer_size=50000, sample_size=2048,
                 seed=0, is_primary=False, path=None, **kwargs):
        super(Worker, self).__init__()
        self.make = make
        self.env_name = env_name

        self.replay_buffer_size = replay_buffer_size
        self.sample_size = sample_size
        self.seed = seed
        self.kwargs = kwargs

        self.is_primary = is_primary
        self.daemon = True
        self.pipe, self.worker_pipe = multiprocessing.Pipe()

        from logger import set_logger
        if path is None and self.is_primary:
            # save the model if it's the primary
            import datetime
            date = str(datetime.datetime.now()).split('.')[0]
            path = f'/tmp/AWR/{env_name}_{date}'

        if not os.path.exists(path):
            os.makedirs(path)

        self.path = path

        if path is not None and path != 'tmp':
            set_logger(os.path.join(path, 'log.txt'))
            import logging
            self.print = logging.info
        else:
            self.print = print
        self.start()

    def run(self):
        # initialize
        env = self.make(self.env_name)
        env.seed(self.seed)
        env.reset()

        import random
        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)

        agent = AsyncDDPGAgent(env.observation_space, env.action_space, **self.kwargs, pipe=self.worker_pipe)
        agent.seed = self.seed

        states = deque(maxlen=self.replay_buffer_size)
        actions = deque(maxlen=self.replay_buffer_size)
        rewards = deque(maxlen=self.replay_buffer_size)
        dones = deque(maxlen=self.replay_buffer_size)
        episode = 0
        optim_iter = 0

        params = params_std = None

        def reset_env():
            obs = env.reset()
            if params is not None:
                x = params.copy()
                if params_std is not None:
                    x += params_std * np.random.normal(size=params_std.shape)
                from model import set_params
                set_params(env, x.clip(0, 1.05))
            return obs

        while True:
            op, data = self.worker_pipe.recv()
            if op == self.EXIT:
                break
            elif op == self.RESET:
                states = deque(maxlen=self.replay_buffer_size)
                actions = deque(maxlen=self.replay_buffer_size)
                rewards = deque(maxlen=self.replay_buffer_size)
                dones = deque(maxlen=self.replay_buffer_size)
                episode = 0
                optim_iter = 0

            elif op == self.START:
                new_samples, critic_steps, actor_steps = data

                # new rollout
                obs = reset_env()

                episodes = []
                episode_reward = 0
                num = 0
                # --------------- totally prallel ------------------
                while True:
                    # feed the actions into the environment
                    states.append(obs)
                    action = agent.act(obs[None,:])[0]
                    obs, r, done, info = env.step(action)

                    actions.append(action)
                    rewards.append(r)
                    dones.append(False)
                    num += 1

                    episode_reward += r
                    if done:
                        # last transitions..
                        states.append(np.array(obs))
                        actions.append(action)
                        rewards.append(0)
                        dones.append(True)
                        num += 1

                        episode += 1
                        episodes.append(episode_reward)

                        if num >= new_samples:
                            break
                        obs = reset_env()
                        episode_reward=0

                optim_iter += 1
                states_ = np.array(states)
                new_states = states_[-num:] 

                agent.update_normalizer(new_states)

                if self.is_primary:
                    self.print(f'\n {optim_iter}: episode num: {len(episodes)}, reward: {np.mean(episodes)}, average length: {num/len(episodes)}')

                # process weight is 1, ideally it shouldn't, but we don't care about it now ...
                agent.update([states_, actions, rewards, dones], critic_steps, actor_steps, process_weight=1)

                self.worker_pipe.send("FINISH ONE ITER")

                if self.is_primary and optim_iter % 10 == 1:
                    obs = reset_env()
                    reward = 0
                    while True:
                        action = agent.act(obs[None,:], mode='test')[0]
                        obs, r, done, info = env.step(action)
                        reward += r
                        if done:
                            break
                    self.print(f'\n {optim_iter}: test result {reward}')
                    if self.path is not None:
                        agent.save(os.path.join(self.path, 'model.pt'))


            elif op == self.ASK:
                action = agent.act(*data)
                self.worker_pipe.send(action)
            elif op == self.SET_ENV:
                params, params_std = data
            elif op == self.GET_PARAM:
                self.worker_pipe.send(agent.get_params())
            elif op == self.SET_PARAM:
                agent.set_params(data)
            elif op == self.COPY:
                normalizer_dict, critic_dict, actor_dict = data
                agent.critic.load_state_dict(critic_dict)
                agent.actor.load_state_dict(actor_dict)
                agent.normalizer.load_state_dict(normalizer_dict)
                agent.normalizer.size = (int(agent.critic.fc1.weight.shape[-1]),)
            else:
                raise NotImplementedError

    def set_params(self, params):
        self.pipe.send([self.SET_PARAM, params])

    def get_params(self):
        self.pipe.send([self.GET_PARAM, None])
        return self.pipe.recv()

    def send(self, data):
        self.pipe.send(data)

    def act(self, obs, mode='sample'):
        self.pipe.send([self.ASK, [obs, mode]])
        return self.pipe.recv()

    def set_env(self, params, params_std=None):
        self.pipe.send([self.SET_ENV, [params, params_std]])

    def reset(self):
        self.pipe.send([self.RESET, None])

    def recv(self):
        return self.pipe.recv()

    def copy(self, normalizer, critic, actor):
        self.pipe.send([self.COPY,
            [normalizer.state_dict(),
            critic.state_dict(),
            actor.state_dict()]
        ])


class AWR:
    def __init__(self, n, num_iter=10000,
            new_samples=2048,
            critic_update_steps=200,
            actor_update_steps=1000, *args,
            seed=123, **kwargs):

        self.workers = []
        for i in range(n):
            self.workers.append(Worker(*args, **kwargs, seed=seed + i, is_primary=(i==0)))
        self.start(num_iter, new_samples, critic_update_steps, actor_update_steps)

    def start(self, num_iter, new_samples, critic_update_steps, actor_update_steps, sync_weights=True):
        primary = self.workers[0]

        if sync_weights:
            params = primary.get_params()
            for i in self.workers:
                i.set_params(params)

        start_command = [primary.START, [new_samples, critic_update_steps, actor_update_steps]]
        for iter_id in tqdm.trange(num_iter):
            for i in self.workers:
                i.send(start_command)

            self.update_normalizer()

            for i in range(critic_update_steps):
                self.reduce(mode='sum') # for critic
            for i in range(actor_update_steps):
                self.reduce(mode='sum') # for actor

            for i in self.workers:
                out = i.recv()


    def __call__(self, observation):
        self.workers[0].send([self.workers[0].ASK, observation])
        return self.workers[0].recv()

    def reduce(self, mode='mean'):
        grad = self.workers[0].recv()
        for i in self.workers[1:]:
            grad = grad + i.recv()
        if mode == 'mean':
            grad = grad / len(self.workers)
        for i in self.workers:
            i.send(grad)

    def update_normalizer(self):
        s, sq, count = 0, 0, 0
        for i in self.workers:
            data = i.recv()
            _s, _sq, _count = data
            s += _s; sq += _sq; count += _count
        for i in self.workers:
            i.send([s, sq, count])