package com.subhamviky.e2a.workflow;

import java.util.HashSet;
import java.util.List;

/** The ordered steps of a workflow. No loops, parallel branches or graphs. */
public record Steps(List<WorkflowStep> steps) {

    public Steps {
        if (steps.isEmpty()) {
            throw new IllegalArgumentException("a workflow needs at least one step");
        }
        if (new HashSet<>(steps.stream().map(WorkflowStep::name).toList()).size() != steps.size()) {
            throw new IllegalArgumentException("step names must be unique");
        }
        steps = List.copyOf(steps);
    }

    public static Steps of(WorkflowStep... steps) {
        return new Steps(List.of(steps));
    }

    /** Index of the first material step (the claim is taken before it); -1 when none is material. */
    public int claimAt() {
        for (int i = 0; i < steps.size(); i++) {
            if (steps.get(i).material()) {
                return i;
            }
        }
        return -1;
    }
}
