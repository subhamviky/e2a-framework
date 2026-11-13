package com.subhamviky.e2a.workflow;

import com.subhamviky.e2a.agent.BaseAgent;

import java.util.Map;
import java.util.function.Predicate;

/** One agent run. */
public record Step(String name, BaseAgent agent, Predicate<Map<String, Object>> when,
                   Predicate<Map<String, Object>> stopIf, boolean material) implements WorkflowStep {

    public static Step of(String name, BaseAgent agent) {
        return new Step(name, agent, null, null, false);
    }

    public Step materialStep() {
        return new Step(name, agent, when, stopIf, true);
    }

    public Step onlyWhen(Predicate<Map<String, Object>> condition) {
        return new Step(name, agent, condition, stopIf, material);
    }

    public Step stopWhen(Predicate<Map<String, Object>> condition) {
        return new Step(name, agent, when, condition, material);
    }

    @Override
    public BaseAgent agentFor(Map<String, Object> state) {
        return agent;
    }
}
