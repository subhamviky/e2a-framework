package com.subhamviky.e2a.core;

/**
 * One owner per tenant-scoped idempotency key. The claim is atomic: two concurrent callers can
 * never both get CLAIMED. The same key with a different fingerprint is a CONFLICT; a key whose
 * work failed may be claimed again.
 */
public interface IdempotencyStore {

    Claim claim(String tenantId, String key, String fingerprint);

    void complete(String tenantId, String key, int status, String body);

    void fail(String tenantId, String key);
}
