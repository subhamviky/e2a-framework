package com.subhamviky.e2a.governance;

import com.subhamviky.e2a.core.GovernanceRefusal;
import com.subhamviky.e2a.core.RequestContext;

import java.util.Map;

/** One governance check. A failure is a typed {@link GovernanceRefusal}, never a boolean. */
public interface Stage {

    default Phase phase() {
        return Phase.SCREEN;
    }

    default String name() {
        return getClass().getSimpleName();
    }

    void check(Map<String, Object> state, RequestContext ctx) throws GovernanceRefusal;
}
