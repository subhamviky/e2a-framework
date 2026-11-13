package com.subhamviky.e2a.test;

import com.subhamviky.e2a.E2AConfig;
import com.subhamviky.e2a.agent.BaseAgent;
import com.subhamviky.e2a.core.Deadline;
import com.subhamviky.e2a.core.RequestContext;
import com.subhamviky.e2a.llm.LlmService;
import com.subhamviky.e2a.llm.SimulatedLlmService;
import com.subhamviky.e2a.tool.ToolService;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The guarantees every E2A agent keeps (the Java counterpart of e2a.conformance.AgentContract).
 * Extend it once per agent:
 *
 * <pre>
 * class OrderAgentContractTest extends AgentContract {
 *     protected BaseAgent agent() { return new OrderAgent(); }
 *     protected Map&lt;String, Object&gt; validState() { return Map.of("order_id", "o-1"); }
 *     protected E2AConfig config() { return E2AConfig.empty().withTool("submit_payment", new FakePayment()); }
 * }
 * </pre>
 */
public abstract class AgentContract {

    /** A fresh agent under test. */
    protected abstract BaseAgent agent();

    /** A request the agent accepts. */
    protected abstract Map<String, Object> validState();

    /** The tools (fakes) and model the agent runs with. Default: a simulated model answering "{}". */
    protected E2AConfig config() {
        return E2AConfig.empty().withLlm(new SimulatedLlmService("{}"));
    }

    private final List<String> materialCalls = new CopyOnWriteArrayList<>();

    private E2AConfig recording(E2AConfig base, boolean failing) {
        Map<String, ToolService> tools = new LinkedHashMap<>();
        base.tools().forEach((name, tool) -> tools.put(name, new ToolService() {
            @Override
            public boolean material() {
                return tool.material();
            }

            @Override
            public Map<String, Object> execute(Map<String, Object> payload, RequestContext ctx, String key) throws Exception {
                if (tool.material()) {
                    materialCalls.add(name);
                }
                if (failing) {
                    throw new IllegalStateException("contract test: tool " + name + " fails");
                }
                return tool.execute(payload, ctx, key);
            }
        }));
        return base.withTools(tools);
    }

    private Map<String, Object> state() {
        return new HashMap<>(validState());
    }

    @Test
    void runNeverThrowsWhenTheModelAndEveryToolFail() {
        LlmService broken = (messages, ctx) -> {
            throw new IllegalStateException("contract test: the model fails");
        };
        E2AConfig failing = recording(config(), true).withLlm(broken);
        assertDoesNotThrow(() -> agent().run(state(), RequestContext.begin("contract"), failing));
    }

    @Test
    void aModelAnswerBelowMinConfidenceActsOnNothing() {
        RequestContext strict = RequestContext.begin("contract").withSettings(Map.of("min_confidence", 1.01));
        Map<String, Object> result = agent().run(state(), strict, recording(config(), false));
        Assumptions.assumeTrue(result.containsKey("evaluation"), "a deterministic agent has no model step");
        assertEquals("FALLBACK", result.get("evaluation"));
        assertTrue(materialCalls.isEmpty(), "a fallback called a material tool: " + materialCalls);
    }

    @Test
    void nothingMaterialRunsAfterTheDeadline() {
        RequestContext late = RequestContext.begin("contract").withDeadline(Deadline.after(Duration.ofMillis(-1)));
        agent().run(state(), late, recording(config(), false));
        assertTrue(materialCalls.isEmpty(), "a material tool ran after the deadline: " + materialCalls);
    }

    @Test
    void theRequestTenantReachesTheResult() {
        Map<String, Object> result = agent().run(state(), RequestContext.begin("tenant-under-test"), recording(config(), false));
        assertEquals("tenant-under-test", result.get("tenant_id"));
    }
}
