package com.subhamviky.e2a.tool;

import com.subhamviky.e2a.core.RequestContext;

import java.util.Map;
import java.util.Set;

/** io_read: replay-safe tools only. A read commits nothing. */
public final class ReadAccess extends ToolAccess {

    public ReadAccess(RequestContext ctx, Map<String, ToolService> tools, Set<String> allowed) {
        super(ctx, tools, allowed);
    }

    @Override
    boolean material() {
        return false;
    }

    public ToolResult call(String tool, Map<String, Object> payload) {
        return invoke(tool, payload, null);
    }
}
