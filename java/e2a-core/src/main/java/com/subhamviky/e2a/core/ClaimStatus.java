package com.subhamviky.e2a.core;

/** Outcome of an idempotency claim. */
public enum ClaimStatus {
    CLAIMED,      // the caller owns the work: execute it
    REPLAY,       // already succeeded: return the stored result
    CONFLICT,     // same key, different request content
    IN_PROGRESS   // another caller holds the claim right now
}
