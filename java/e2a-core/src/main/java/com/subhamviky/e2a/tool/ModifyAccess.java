package com.subhamviky.e2a.tool;

import com.subhamviky.e2a.core.RequestContext;

import java.util.Map;
import java.util.Set;

/**
 * io_modify: material tools only, always with an idempotency key (derived from the request's key
 * and the tool name when none is given). A transport failure is UNKNOWN, never a silent retry.
 */
public final class ModifyAccess extends ToolAccess {

    public ModifyAccess(RequestContext ctx, Map<String, ToolService> tools, Set<String> allowed) {
        super(ctx, tools, allowed);
    }

    @Override
    boolean material() {
        return true;
    }

    public ToolResult call(String tool, Map<String, Object> payload) {
        String base = ctx().idempotencyKey() != null ? ctx().idempotencyKey() : ctx().correlationId();
        return invoke(tool, payload, base + ":" + tool);
    }

    public ToolResult call(String tool, Map<String, Object> payload, String idempotencyKey) {
        return invoke(tool, payload, idempotencyKey);
    }
}
