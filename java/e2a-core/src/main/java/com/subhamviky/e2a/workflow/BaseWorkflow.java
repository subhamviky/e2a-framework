package com.subhamviky.e2a.workflow;

import com.subhamviky.e2a.E2AConfig;
import com.subhamviky.e2a.agent.BaseAgent;
import com.subhamviky.e2a.core.ApprovalRequired;
import com.subhamviky.e2a.core.Claim;
import com.subhamviky.e2a.core.IdempotencyStore;
import com.subhamviky.e2a.core.RequestContext;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * A workflow of declared steps (Python e2a.BaseWorkflow with Steps, design note 05). {@link #execute}
 * is final: all steps run under one request context and one idempotency claim, taken before the
 * first material step, so a read-only request takes none and a repeat of a material one is
 * REPLAYED as a whole. Each step is recorded in {@code state.steps}; a fallback or a refusal ends
 * the workflow; an approval pauses it ({@code status AWAITING_APPROVAL}).
 */
public abstract class BaseWorkflow {

    private static final Set<String> BOOKKEEPING = Set.of("steps", "status", "response", "outcome", "refusal",
            "errors", "evaluation", "confidence", "tokens_used", "latency_ms", "correlation_id", "replayed",
            "approval_granted", "deadline_exceeded");

    public final Map<String, Object> execute(Map<String, Object> state, RequestContext ctx, E2AConfig config) {
        Steps steps = buildWorkflow(ctx);
        List<Map<String, Object>> records = new ArrayList<>();
        state.put("steps", records);
        IdempotencyStore store = config.idempotencyStore();
        int claimAt = steps.claimAt();
        String key = null;
        boolean claimed = false;
        String status = "COMPLETED";
        try {
            for (int i = 0; i < steps.steps().size(); i++) {
                WorkflowStep step = steps.steps().get(i);
                if (i == claimAt && store != null) {
                    key = ctx.idempotencyKey() != null ? ctx.idempotencyKey() : generateIdempotencyKey(state, ctx);
                    ctx = ctx.withIdempotencyKey(key);
                    Claim claim = store.claim(ctx.tenantId(), key, requestFingerprint(state, ctx));
                    switch (claim.status()) {
                        case REPLAY -> {
                            state.put("response", claim.stored().body());
                            state.put("replayed", true);
                            state.put("status", "REPLAYED");
                            return state;
                        }
                        case CONFLICT, IN_PROGRESS -> {
                            state.put("status", claim.status().name());
                            return state;
                        }
                        default -> claimed = true;
                    }
                }
                if (step.when() != null && !step.when().test(state)) {
                    records.add(record(step.name(), "-", "SKIPPED", 0));
                    continue;
                }
                BaseAgent agent = step.agentFor(state);
                long start = System.nanoTime();
                try {
                    agent.run(state, ctx, config);
                } catch (ApprovalRequired pause) {
                    records.add(record(step.name(), agent.getClass().getSimpleName(), "PAUSED", start));
                    status = "AWAITING_APPROVAL";
                    break;
                }
                String outcome = state.containsKey("refusal") ? "REFUSED"
                        : "FALLBACK".equals(state.get("evaluation")) ? "FALLBACK"
                        : state.containsKey("errors") ? "FAILED" : "DONE";
                records.add(record(step.name(), agent.getClass().getSimpleName(), outcome, start));
                if (!"DONE".equals(outcome)) {
                    status = outcome;
                    break;
                }
                if (step.stopIf() != null && step.stopIf().test(state)) {
                    break;
                }
            }
        } catch (RuntimeException error) {
            status = "FAILED";
            handleError(error, state, ctx);
        }
        if ("COMPLETED".equals(status) && "UNKNOWN".equals(state.get("outcome"))) {
            status = "RECOVERY_REQUIRED";                 // never replay or retry an unknown outcome blindly
        }
        state.put("status", status);
        if (claimed) {
            if ("COMPLETED".equals(status)) {
                store.complete(ctx.tenantId(), key, 200, replayableResult(state, ctx));
            } else {
                store.fail(ctx.tenantId(), key);
            }
        }
        return state;
    }

    private static Map<String, Object> record(String step, String agent, String status, long start) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("step", step);
        r.put("agent", agent);
        r.put("status", status);
        r.put("latency_ms", start == 0 ? 0 : (System.nanoTime() - start) / 1_000_000);
        return r;
    }

    /** The declared steps. */
    protected abstract Steps buildWorkflow(RequestContext ctx);

    /** A deterministic key from the business content, when the client sent none. Needed for a material step. */
    protected String generateIdempotencyKey(Map<String, Object> state, RequestContext ctx) {
        throw new IllegalStateException(getClass().getSimpleName()
                + " has a material step and the request has no idempotency key: implement generateIdempotencyKey");
    }

    /** Same key + a different fingerprint = CONFLICT. Default: SHA-256 of the business fields of the state. */
    protected String requestFingerprint(Map<String, Object> state, RequestContext ctx) {
        Map<String, Object> business = new TreeMap<>();
        state.forEach((k, v) -> {
            if (!BOOKKEEPING.contains(k)) {
                business.put(k, v);
            }
        });
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(business.toString().getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** What a replay returns. Default: the response. */
    protected String replayableResult(Map<String, Object> state, RequestContext ctx) {
        return String.valueOf(state.get("response"));
    }

    /** Map a failure to the caller's shape. Must not throw. */
    protected void handleError(RuntimeException error, Map<String, Object> state, RequestContext ctx) {
        state.put("error", error.getClass().getSimpleName() + ": " + error.getMessage());
    }
}
