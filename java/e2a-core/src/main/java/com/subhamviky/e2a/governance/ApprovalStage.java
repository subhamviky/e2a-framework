package com.subhamviky.e2a.governance;

import com.subhamviky.e2a.core.ApprovalRequired;
import com.subhamviky.e2a.core.RequestContext;

import java.util.Map;
import java.util.function.BiPredicate;

/**
 * Human approval, in the DECIDE phase (after the agent read the facts the decision is about).
 * When approval is required and {@code state.approval_granted} is not true, the step pauses.
 */
public final class ApprovalStage implements Stage {

    private final BiPredicate<Map<String, Object>, RequestContext> required;

    public ApprovalStage(BiPredicate<Map<String, Object>, RequestContext> required) {
        this.required = required;
    }

    @Override
    public Phase phase() {
        return Phase.DECIDE;
    }

    @Override
    public void check(Map<String, Object> state, RequestContext ctx) {
        if (required.test(state, ctx) && !Boolean.TRUE.equals(state.get("approval_granted"))) {
            throw new ApprovalRequired("approval required before this step acts");
        }
    }
}
