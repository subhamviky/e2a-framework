package com.subhamviky.e2a.cqrs;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.subhamviky.e2a.core.Claim;
import com.subhamviky.e2a.core.ClaimStatus;
import com.subhamviky.e2a.core.IdempotencyStore;
import com.subhamviky.e2a.core.InMemoryIdempotencyStore;
import jakarta.validation.Validation;
import jakarta.validation.Validator;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;
import org.junit.jupiter.api.Test;

import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class CqrsProfileTest {

    record Pay(@NotBlank String account, @Positive int amount) {
    }

    static final Validator VALIDATOR = Validation.buildDefaultValidatorFactory().getValidator();
    final AtomicInteger executions = new AtomicInteger();

    class PayService extends BaseCommandService<Pay> {
        PayService() {
            super(VALIDATOR, 1000);
        }

        @Override
        protected CqrsResult executeBusinessLogic(Pay input, CommandContext context) {
            executions.incrementAndGet();
            if (input.amount() == 13) {
                throw new IllegalStateException("unlucky");
            }
            return CqrsResult.of(201, Map.of("paid", input.amount(), "tenant", context.tenantId()));
        }
    }

    static class Balance extends BaseQueryService<String, Map<String, Object>> {
        @Override
        protected Optional<Map<String, Object>> executeDbRead(String id, String tenantId) {
            return "a1".equals(id) ? Optional.of(Map.of("id", id, "tenant", tenantId)) : Optional.empty();
        }
    }

    class Orchestrator extends BaseOrchestrator {
        Orchestrator(IdempotencyStore store) {
            super(store, new ObjectMapper());
        }

        @Override
        protected boolean isReadScenario(CqrsRequest request) {
            return request.action().startsWith("get");
        }

        @Override
        protected BaseQueryService<?, ?> getQueryService(String action) {
            return new Balance();
        }

        @Override
        protected BaseCommandService<?> getCommandService(String action) {
            return new PayService();
        }
    }

    static CqrsRequest pay(String key, int amount) {
        return CqrsRequest.command("pay", new CommandContext("acme", key, "c1"), new Pay("ACC-1", amount));
    }

    @Test
    void aCommandRunsOnceAndARepeatIsReplayed() {
        Orchestrator o = new Orchestrator(new InMemoryIdempotencyStore());
        CqrsResult first = o.execute(pay("k1", 10));
        CqrsResult again = o.execute(pay("k1", 10));
        assertEquals(201, first.httpStatus());
        assertFalse(first.replayed());
        assertTrue(again.replayed());
        assertEquals(201, again.httpStatus());
        assertEquals(10, ((com.fasterxml.jackson.databind.JsonNode) again.body()).get("paid").asInt());
        assertEquals(1, executions.get());
    }

    @Test
    void theSameKeyWithADifferentCommandIsAConflict() {
        Orchestrator o = new Orchestrator(new InMemoryIdempotencyStore());
        o.execute(pay("k1", 10));
        assertEquals(409, o.execute(pay("k1", 11)).httpStatus());
    }

    @Test
    void aClaimHeldByAnotherCallerIsInProgress() {
        InMemoryIdempotencyStore store = new InMemoryIdempotencyStore();
        Orchestrator o = new Orchestrator(store);
        String fingerprint = o.requestFingerprint(pay("k1", 10));
        assertEquals(ClaimStatus.CLAIMED, store.claim("acme", "k1", fingerprint).status());
        CqrsResult busy = o.execute(pay("k1", 10));
        assertEquals(409, busy.httpStatus());
        assertTrue(busy.body().toString().contains("still in progress"));
    }

    @Test
    void aFailedCommandReleasesTheKeyForARetry() {
        Orchestrator o = new Orchestrator(new InMemoryIdempotencyStore());
        assertThrows(IllegalStateException.class, () -> o.execute(pay("k2", 13)));
        assertThrows(IllegalStateException.class, () -> o.execute(pay("k2", 13)));
        assertEquals(2, executions.get(), "the retry executed again: the failure did not stick to the key");
    }

    @Test
    void anInvalidCommandIsA400WithEveryViolation() {
        CqrsResult r = new Orchestrator(new InMemoryIdempotencyStore())
                .execute(CqrsRequest.command("pay", new CommandContext("acme", "k3", "c1"), new Pay("", -1)));
        assertEquals(400, r.httpStatus());
        assertEquals(2, ((java.util.List<?>) ((Map<?, ?>) r.body()).get("errors")).size());
        assertEquals(0, executions.get());
    }

    @Test
    void aQueryIsTenantScopedAndTakesNoClaim() {
        IdempotencyStore never = new IdempotencyStore() {
            public Claim claim(String t, String k, String f) {
                throw new AssertionError("a query took a claim");
            }

            public void complete(String t, String k, int s, String b) {
            }

            public void fail(String t, String k) {
            }
        };
        Orchestrator o = new Orchestrator(never);
        CqrsResult found = o.execute(CqrsRequest.query("get", new CommandContext("acme", null, "c1"), Map.of("id", "a1")));
        assertEquals(200, found.httpStatus());
        assertEquals("acme", ((Map<?, ?>) found.body()).get("tenant"));
        assertEquals(404, o.execute(CqrsRequest.query("get", new CommandContext("acme", null, "c1"), Map.of("id", "zz"))).httpStatus());
    }
}
