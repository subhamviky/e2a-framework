package com.subhamviky.e2a.workflow;

import com.subhamviky.e2a.agent.BaseAgent;

import java.util.Map;
import java.util.function.Predicate;

/** One agent run, chosen by a state field: {@code agents.get(state.get(by))}, else the default. */
public record Route(String name, String by, Map<String, BaseAgent> agents, BaseAgent defaultAgent,
                    Predicate<Map<String, Object>> when, Predicate<Map<String, Object>> stopIf,
                    boolean material) implements WorkflowStep {

    public Route {
        agents = Map.copyOf(agents);
    }

    public static Route by(String name, String field, Map<String, BaseAgent> agents) {
        return new Route(name, field, agents, null, null, null, false);
    }

    public Route materialStep() {
        return new Route(name, by, agents, defaultAgent, when, stopIf, true);
    }

    public Route otherwise(BaseAgent agent) {
        return new Route(name, by, agents, agent, when, stopIf, material);
    }

    @Override
    public BaseAgent agentFor(Map<String, Object> state) {
        BaseAgent agent = agents.getOrDefault(String.valueOf(state.get(by)), defaultAgent);
        if (agent == null) {
            throw new IllegalArgumentException("step '" + name + "': no agent for " + by + "=" + state.get(by));
        }
        return agent;
    }
}
