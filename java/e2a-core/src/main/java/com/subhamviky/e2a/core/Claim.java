package com.subhamviky.e2a.core;

/** Result of {@link IdempotencyStore#claim}; {@code stored} is set for {@link ClaimStatus#REPLAY}. */
public record Claim(ClaimStatus status, StoredResult stored) {

    public static Claim of(ClaimStatus status) {
        return new Claim(status, null);
    }

    /** The result kept for a completed key: a status code and a serialised body. */
    public record StoredResult(int status, String body) {
    }
}
