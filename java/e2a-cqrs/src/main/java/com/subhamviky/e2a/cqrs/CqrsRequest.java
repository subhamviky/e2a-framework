package com.subhamviky.e2a.cqrs;

import java.util.Map;

/** One request into {@link BaseOrchestrator#execute}: an action name, the context and the payload. */
public record CqrsRequest(String action, CommandContext context, Object payload, Map<String, String> params) {

    public static CqrsRequest command(String action, CommandContext context, Object payload) {
        return new CqrsRequest(action, context, payload, Map.of());
    }

    public static CqrsRequest query(String action, CommandContext context, Map<String, String> params) {
        return new CqrsRequest(action, context, null, params);
    }
}
