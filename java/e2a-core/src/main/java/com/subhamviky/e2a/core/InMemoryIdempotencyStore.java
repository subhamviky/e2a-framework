package com.subhamviky.e2a.core;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** A thread-safe {@link IdempotencyStore} in memory, for tests and single-instance tools. */
public final class InMemoryIdempotencyStore implements IdempotencyStore {

    private enum State { IN_PROGRESS, SUCCEEDED, FAILED }

    private record Entry(String fingerprint, State state, Claim.StoredResult result) {
    }

    private final Map<String, Entry> entries = new ConcurrentHashMap<>();

    private static String id(String tenantId, String key) {
        return tenantId + "\u0000" + key;
    }

    @Override
    public Claim claim(String tenantId, String key, String fingerprint) {
        Claim[] out = new Claim[1];
        entries.compute(id(tenantId, key), (k, existing) -> {
            if (existing == null || (existing.state() == State.FAILED && existing.fingerprint().equals(fingerprint))) {
                out[0] = Claim.of(ClaimStatus.CLAIMED);
                return new Entry(fingerprint, State.IN_PROGRESS, null);
            }
            if (!existing.fingerprint().equals(fingerprint)) {
                out[0] = Claim.of(ClaimStatus.CONFLICT);
            } else if (existing.state() == State.SUCCEEDED) {
                out[0] = new Claim(ClaimStatus.REPLAY, existing.result());
            } else {
                out[0] = Claim.of(ClaimStatus.IN_PROGRESS);
            }
            return existing;
        });
        return out[0];
    }

    @Override
    public void complete(String tenantId, String key, int status, String body) {
        entries.computeIfPresent(id(tenantId, key),
                (k, e) -> new Entry(e.fingerprint(), State.SUCCEEDED, new Claim.StoredResult(status, body)));
    }

    @Override
    public void fail(String tenantId, String key) {
        entries.computeIfPresent(id(tenantId, key), (k, e) -> new Entry(e.fingerprint(), State.FAILED, null));
    }
}
