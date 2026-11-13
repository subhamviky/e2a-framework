package com.subhamviky.e2a.governance;

import com.subhamviky.e2a.core.RequestContext;

import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The governance of a service: ordered stages and, optionally, which tools each agent may use.
 * Stages of a phase run in order; the first refusal stops the request.
 */
public final class Governance {

    private final List<Stage> stages;
    private final Map<String, Set<String>> toolPolicy;    // agent name -> tools; absent agent = no limit

    public Governance(List<Stage> stages, Map<String, Set<String>> toolPolicy) {
        this.stages = List.copyOf(stages);
        this.toolPolicy = Map.copyOf(toolPolicy);
    }

    public static Governance none() {
        return new Governance(List.of(), Map.of());
    }

    public void enforce(Phase phase, Map<String, Object> state, RequestContext ctx) {
        for (Stage stage : stages) {
            if (stage.phase() == phase) {
                stage.check(state, ctx);
            }
        }
    }

    /** The tools an agent may call; null when the policy does not name the agent. */
    public Set<String> allowedTools(String agent) {
        return toolPolicy.get(agent);
    }

    public List<Stage> stages() {
        return stages;
    }
}
