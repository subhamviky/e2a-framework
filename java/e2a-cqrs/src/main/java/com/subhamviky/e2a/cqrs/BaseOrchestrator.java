package com.subhamviky.e2a.cqrs;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.subhamviky.e2a.core.Claim;
import com.subhamviky.e2a.core.IdempotencyStore;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

/**
 * E2A Deterministic CQRS profile, tier-2 lifecycle (Java realisation of
 * e2a_cqrs_base.BaseOrchestrator; e2a-java since 1.2.2). Routes reads to a query service and writes
 * to a command service behind an atomic idempotency claim. Never calls a model.
 */
public abstract class BaseOrchestrator {

    private final IdempotencyStore store;
    private final ObjectMapper json;

    protected BaseOrchestrator(IdempotencyStore store, ObjectMapper json) {
        this.store = store;
        this.json = json;
    }

    /** Single public entry point. */
    public final CqrsResult execute(CqrsRequest request) {
        if (isReadScenario(request)) {
            return runQuery(request);
        }
        CommandContext ctx = request.context();
        String fingerprint = requestFingerprint(request);
        Claim claim = store.claim(ctx.tenantId(), ctx.idempotencyKey(), fingerprint);
        switch (claim.status()) {
            case REPLAY:
                return new CqrsResult(claim.stored().status(), parse(claim.stored().body()), true);
            case CONFLICT:
                return CqrsResult.error(409, "Idempotency-Key was already used with a different request");
            case IN_PROGRESS:
                return CqrsResult.error(409, "A request with this Idempotency-Key is still in progress");
            default:
                break;
        }
        try {
            CqrsResult result = runCommand(request);
            store.complete(ctx.tenantId(), ctx.idempotencyKey(), result.httpStatus(), write(result.body()));
            return result;
        } catch (RuntimeException e) {
            store.fail(ctx.tenantId(), ctx.idempotencyKey());      // retryable: the client may resend the same key
            handleError(e, request);
            throw e;
        }
    }

    protected abstract boolean isReadScenario(CqrsRequest request);

    protected abstract BaseQueryService<?, ?> getQueryService(String action);

    protected abstract BaseCommandService<?> getCommandService(String action);

    /** Hook for logging or metrics on failures; the exception is re-thrown after it. */
    protected void handleError(RuntimeException error, CqrsRequest request) {
    }

    /** Stable hash of the command's business content: same key + different content = CONFLICT. */
    protected String requestFingerprint(CqrsRequest request) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest((request.action() + "|" + write(request.payload())).getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private CqrsResult runCommand(CqrsRequest request) {
        BaseCommandService service = getCommandService(request.action());
        return service.mutate(request.payload(), request.context());
    }

    @SuppressWarnings("unchecked")
    private CqrsResult runQuery(CqrsRequest request) {
        BaseQueryService<String, Object> service = (BaseQueryService<String, Object>) getQueryService(request.action());
        return service.fetch(request.params().get("id"), request.context().tenantId())
                .map(body -> CqrsResult.of(200, body))
                .orElseGet(() -> CqrsResult.error(404, "not found"));
    }

    private String write(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private Object parse(String body) {
        try {
            return json.readTree(body);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
