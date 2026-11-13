package com.subhamviky.e2a.tool;

import com.subhamviky.e2a.core.PhysicalOutcome;

import java.util.Map;

/** A tool's typed result: the physical outcome and the response body. */
public record ToolResult(String tool, PhysicalOutcome outcome, Map<String, Object> body) {

    public ToolResult {
        body = body == null ? Map.of() : Map.copyOf(body);
    }

    public Object get(String key) {
        return body.get(key);
    }
}
