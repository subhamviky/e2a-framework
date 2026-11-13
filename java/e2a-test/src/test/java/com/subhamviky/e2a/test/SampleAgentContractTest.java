package com.subhamviky.e2a.test;

import com.subhamviky.e2a.E2AConfig;
import com.subhamviky.e2a.agent.BaseAgent;
import com.subhamviky.e2a.core.RequestContext;
import com.subhamviky.e2a.llm.LlmResponse;
import com.subhamviky.e2a.llm.SimulatedLlmService;
import com.subhamviky.e2a.tool.ModifyAccess;
import com.subhamviky.e2a.tool.ToolResult;
import com.subhamviky.e2a.tool.ToolService;

import java.util.List;
import java.util.Map;

/** The kit, run against a sample agent with a model step and a material tool. */
class SampleAgentContractTest extends AgentContract {

    static class RefundAgent extends BaseAgent {
        @Override
        protected List<Map<String, String>> buildMessages(Map<String, Object> state, RequestContext ctx) {
            return List.of(Map.of("role", "user", "content", "refund " + state.get("order_id")));
        }

        @Override
        protected double evaluateOutput(LlmResponse response, Map<String, Object> state, RequestContext ctx) {
            return response.text().contains("APPROVE") ? 1.0 : 0.0;
        }

        @Override
        protected Object fallback(Map<String, Object> state, RequestContext ctx) {
            return "a person will review this refund";
        }

        @Override
        protected List<ToolResult> act(Map<String, Object> state, RequestContext ctx, ModifyAccess io) {
            return List.of(io.call("issue_refund", Map.of("order_id", state.get("order_id"))));
        }
    }

    @Override
    protected BaseAgent agent() {
        return new RefundAgent();
    }

    @Override
    protected Map<String, Object> validState() {
        return Map.of("order_id", "o-1");
    }

    @Override
    protected E2AConfig config() {
        ToolService refund = new ToolService() {
            public boolean material() {
                return true;
            }

            public Map<String, Object> execute(Map<String, Object> payload, RequestContext ctx, String key) {
                return Map.of("refunded", true);
            }
        };
        return E2AConfig.empty().withLlm(new SimulatedLlmService("APPROVE")).withTool("issue_refund", refund);
    }
}
