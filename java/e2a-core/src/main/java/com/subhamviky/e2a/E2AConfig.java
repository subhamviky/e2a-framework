package com.subhamviky.e2a;

import com.subhamviky.e2a.core.IdempotencyStore;
import com.subhamviky.e2a.governance.Governance;
import com.subhamviky.e2a.llm.LlmService;
import com.subhamviky.e2a.tool.ToolService;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * What a request runs with: the model gateway, the governance, the tools and the idempotency
 * store. Built once at the composition root and shared; request data never lives here.
 */
public record E2AConfig(LlmService llm, Governance governance, Map<String, ToolService> tools,
                        IdempotencyStore idempotencyStore) {

    public E2AConfig {
        governance = governance == null ? Governance.none() : governance;
        tools = Map.copyOf(tools == null ? Map.of() : tools);
    }

    public static E2AConfig empty() {
        return new E2AConfig(null, null, Map.of(), null);
    }

    public E2AConfig withLlm(LlmService value) {
        return new E2AConfig(value, governance, tools, idempotencyStore);
    }

    public E2AConfig withGovernance(Governance value) {
        return new E2AConfig(llm, value, tools, idempotencyStore);
    }

    public E2AConfig withTool(String name, ToolService tool) {
        Map<String, ToolService> all = new LinkedHashMap<>(tools);
        all.put(name, tool);
        return new E2AConfig(llm, governance, all, idempotencyStore);
    }

    public E2AConfig withTools(Map<String, ToolService> value) {
        return new E2AConfig(llm, governance, value, idempotencyStore);
    }

    public E2AConfig withIdempotencyStore(IdempotencyStore value) {
        return new E2AConfig(llm, governance, tools, value);
    }
}
