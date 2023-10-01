import numpy as np
from agents.content_market_agent import ContentMarketAgent
from models.model import ObjectiveModel
from environments.env import ObjectiveEnv, convert_to_content_market
from typing import cast

class BasicContentMarketModel(ObjectiveModel):

    def __init__(self, env: ObjectiveEnv, num_outputs, model_config, name):
        super().__init__(env, num_outputs, model_config, name)
        self.env = convert_to_content_market(env)
        self.agents_dict = cast(dict[str, ContentMarketAgent], self.env.agents)


    def reassign_agent_objectives(self):
        """
        Update agent production and following rates to optimize towards market equilibrium.
        """
        agents = self.agents_dict.values()
        # TODO: update agent following rates
        # TODO: update agent production rates (objective probabilities)
        return super().reassign_agent_objectives()
    
    def predict(self, obs, **kwargs):
        """
        Sample agent actions from agent production rates
        i.e. for each agent, choose objective with probability equal to agent's production rate
        and then greedily move agent towards chosen objective
        """
        self.reassign_agent_objectives()
        actions = np.zeros((1, self.num_outputs, self.num_actions))
        for agent_id in self.agents_dict:
            agent = self.agents_dict[agent_id]
            cumulative_objective_probs = np.cumsum(list(agent.objective_probs.values()))
            objective_ind = np.searchsorted(cumulative_objective_probs, np.random.rand())
            objective = list(agent.objective_probs.keys())[objective_ind]

            action = self.env.get_greedy_action(agent, objective)[0]
            actions[0][int(agent_id)][action] = 1
        return actions
