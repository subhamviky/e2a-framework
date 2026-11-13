package com.subhamviky.e2a.agent;

import com.subhamviky.e2a.E2AConfig;
import com.subhamviky.e2a.core.ApprovalRequired;
import com.subhamviky.e2a.core.GovernanceRefusal;
import com.subhamviky.e2a.core.PhysicalOutcome;
import com.subhamviky.e2a.core.RequestContext;
import com.subhamviky.e2a.governance.Phase;
import com.subhamviky.e2a.llm.LlmResponse;
import com.subhamviky.e2a.tool.ModifyAccess;
import com.subhamviky.e2a.tool.ReadAccess;
import com.subhamviky.e2a.tool.ToolResult;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * An E2A agent with the v1.2 lifecycle (Python e2a.BaseAgent, design notes 02-04). {@link #run}
 * is final; a subclass fills in protected hooks:
 *
 * <pre>
 * screen stages -> prepare(io_read) -> decide stages -> applyPolicy -> buildMessages (null = no model step)
 * -> model -> evaluateOutput >= minConfidence, else fallback -> act(io_modify) -> confirm -> state.outcome
 * </pre>
 *
 * Effects happen only in {@link #act}, only after an accepted evaluation and before the deadline.
 * {@code run} never throws on a hook error: a refusal goes to {@code state.refusal}, an error to
 * {@code state.errors}. An {@link ApprovalRequired} crosses upward: the workflow pauses the step.
 */
public abstract class BaseAgent {

    private static final Logger log = LoggerFactory.getLogger(BaseAgent.class);

    public final Map<String, Object> run(Map<String, Object> state, RequestContext ctx, E2AConfig config) {
        long start = System.nanoTime();
        config = config == null ? E2AConfig.empty() : config;
        state.put("tenant_id", ctx.tenantId());
        state.put("correlation_id", ctx.correlationId());
        Set<String> allowed = config.governance().allowedTools(name());
        try {
            config.governance().enforce(Phase.SCREEN, state, ctx);
            prepare(state, ctx, new ReadAccess(ctx, config.tools(), allowed));
            config.governance().enforce(Phase.DECIDE, state, ctx);
            applyPolicy(state, ctx);
            boolean accepted = true;
            List<Map<String, String>> messages = buildMessages(state, ctx);
            if (messages != null) {
                if (config.llm() == null) {
                    throw new IllegalStateException(name() + " has a model step but the config has no LlmService");
                }
                LlmResponse response = config.llm().infer(messages, ctx);
                state.put("tokens_used", response.tokensUsed());
                double score = evaluateOutput(response, state, ctx);
                accepted = score >= minConfidence(ctx);
                state.put("confidence", score);
                state.put("evaluation", accepted ? "ACCEPTED" : "FALLBACK");
                state.put("response", accepted ? response.text() : fallback(state, ctx));
            }
            if (!accepted) {
                return state;                                  // a fallback acts on nothing
            }
            if (ctx.expired()) {
                state.put("deadline_exceeded", true);          // nothing material after the deadline
                return state;
            }
            List<ToolResult> outcomes = act(state, ctx, new ModifyAccess(ctx, config.tools(), allowed));
            PhysicalOutcome outcome = confirm(state, ctx, outcomes == null ? List.of() : outcomes);
            if (outcome != null) {
                state.put("outcome", outcome.name());
            }
        } catch (ApprovalRequired pause) {
            throw pause;
        } catch (GovernanceRefusal refusal) {
            state.put("refusal", refusal.toMap());
        } catch (Exception error) {
            try {
                handleError(error, state, ctx);
            } catch (RuntimeException ignored) {
                // handleError must not raise; run() stays total
            }
        } finally {
            state.put("latency_ms", (System.nanoTime() - start) / 1_000_000);
            log.debug("agent {} tenant {} correlation {} done", name(), ctx.tenantId(), ctx.correlationId());
        }
        return state;
    }

    /** The agent's name, as the governance tool policy and the workflow records know it. */
    protected String name() {
        return getClass().getSimpleName();
    }

    /** Below this evaluation score a model answer is replaced by {@link #fallback}. */
    protected double minConfidence(RequestContext ctx) {
        return ((Number) ctx.setting("min_confidence", 0.75)).doubleValue();
    }

    /** Reads and retrieval before the prompt, through io_read only. No side effects. */
    protected void prepare(Map<String, Object> state, RequestContext ctx, ReadAccess io) {
    }

    /** Agent-specific policy after the governance stages; throw a GovernanceRefusal to refuse. */
    protected void applyPolicy(Map<String, Object> state, RequestContext ctx) {
    }

    /** The model conversation, or null for a deterministic agent (no model step). */
    protected abstract List<Map<String, String>> buildMessages(Map<String, Object> state, RequestContext ctx);

    /** Confidence in the model answer, in [0, 1]. Required when there is a model step. */
    protected double evaluateOutput(LlmResponse response, Map<String, Object> state, RequestContext ctx) {
        throw new IllegalStateException(name() + " has a model step: implement evaluateOutput");
    }

    /** The degraded answer when the evaluation fails. Required when there is a model step. */
    protected Object fallback(Map<String, Object> state, RequestContext ctx) {
        throw new IllegalStateException(name() + " has a model step: implement fallback");
    }

    /** The material effects, through io_modify only; returns the tools' typed results. */
    protected List<ToolResult> act(Map<String, Object> state, RequestContext ctx, ModifyAccess io) {
        return List.of();
    }

    /** Physical truth from the typed outcomes: UNKNOWN beats NOT_COMMITTED beats COMMITTED; none, no verdict. */
    protected PhysicalOutcome confirm(Map<String, Object> state, RequestContext ctx, List<ToolResult> outcomes) {
        if (outcomes.isEmpty()) {
            return null;
        }
        for (PhysicalOutcome worst : List.of(PhysicalOutcome.UNKNOWN, PhysicalOutcome.NOT_COMMITTED)) {
            if (outcomes.stream().anyMatch(r -> r.outcome() == worst)) {
                return worst;
            }
        }
        return PhysicalOutcome.COMMITTED;
    }

    /** Records the error in {@code state.errors}. Must not throw. */
    @SuppressWarnings("unchecked")
    protected void handleError(Exception error, Map<String, Object> state, RequestContext ctx) {
        List<Map<String, Object>> errors = (List<Map<String, Object>>) state.computeIfAbsent("errors", k -> new ArrayList<>());
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("type", error.getClass().getSimpleName());
        entry.put("message", String.valueOf(error.getMessage()));
        errors.add(entry);
        log.warn("agent {} failed: {}", name(), error.toString());
    }
}
