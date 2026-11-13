package com.subhamviky.e2a.tool;

import com.subhamviky.e2a.core.PhysicalOutcome;
import com.subhamviky.e2a.core.RequestContext;

import java.util.Map;
import java.util.Set;

/** What {@link ReadAccess} and {@link ModifyAccess} share: lookup, kind and policy checks. */
abstract sealed class ToolAccess permits ReadAccess, ModifyAccess {

    private final RequestContext ctx;
    private final Map<String, ToolService> tools;
    private final Set<String> allowed;            // null = every tool

    ToolAccess(RequestContext ctx, Map<String, ToolService> tools, Set<String> allowed) {
        this.ctx = ctx;
        this.tools = tools == null ? Map.of() : tools;
        this.allowed = allowed;
    }

    abstract boolean material();

    ToolResult invoke(String name, Map<String, Object> payload, String idempotencyKey) {
        ToolService tool = tools.get(name);
        if (tool == null) {
            throw new ToolDenied("no tool named '" + name + "'");
        }
        if (allowed != null && !allowed.contains(name)) {
            throw new ToolDenied("tool '" + name + "' is not allowed for this agent");
        }
        if (tool.material() != material()) {
            throw new ToolDenied("tool '" + name + "' is " + (tool.material() ? "material" : "read-only")
                    + "; it cannot be called through " + (material() ? "io_modify" : "io_read"));
        }
        Map<String, Object> body;
        try {
            body = tool.execute(Map.copyOf(payload), ctx, idempotencyKey);
        } catch (Exception transportFailure) {
            PhysicalOutcome outcome = material() ? PhysicalOutcome.UNKNOWN : PhysicalOutcome.NOT_COMMITTED;
            return new ToolResult(name, outcome, Map.of("error", String.valueOf(transportFailure.getMessage())));
        }
        if (!material()) {
            return new ToolResult(name, PhysicalOutcome.NOT_COMMITTED, body);
        }
        Object reported = body == null ? null : body.get("outcome");
        PhysicalOutcome outcome = reported == null ? PhysicalOutcome.COMMITTED : PhysicalOutcome.valueOf(reported.toString());
        return new ToolResult(name, outcome, body);
    }

    RequestContext ctx() {
        return ctx;
    }
}
