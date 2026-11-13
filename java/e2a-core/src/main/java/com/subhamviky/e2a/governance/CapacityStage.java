package com.subhamviky.e2a.governance;

import com.subhamviky.e2a.core.GovernanceRefusal;
import com.subhamviky.e2a.core.RequestContext;

import java.util.Map;
import java.util.function.BooleanSupplier;

/** Load shedding: refuses (retryable) when the admission check says the service is saturated. */
public final class CapacityStage implements Stage {

    private final BooleanSupplier admit;

    public CapacityStage(BooleanSupplier admit) {
        this.admit = admit;
    }

    @Override
    public void check(Map<String, Object> state, RequestContext ctx) {
        if (!admit.getAsBoolean()) {
            throw new GovernanceRefusal("CAPACITY", "capacity", "the service is at capacity; retry later", true);
        }
    }
}
