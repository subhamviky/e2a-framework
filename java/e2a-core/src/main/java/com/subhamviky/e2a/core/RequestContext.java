package com.subhamviky.e2a.core;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.UUID;

/**
 * The identity and settings of one request, created once at the outermost entry point and
 * passed to every hook (E2A v1.2 design note 02). Immutable: a narrower scope is a copy.
 */
public record RequestContext(String tenantId, String correlationId, String idempotencyKey, Deadline deadline,
                             Map<String, Object> settings) {

    public RequestContext {
        if (tenantId == null || tenantId.isBlank()) {
            throw new IllegalArgumentException("a tenantId is required to start a request");
        }
        correlationId = correlationId == null ? UUID.randomUUID().toString() : correlationId;
        settings = Collections.unmodifiableMap(new LinkedHashMap<>(settings == null ? Map.of() : settings));
    }

    public static RequestContext begin(String tenantId) {
        return new RequestContext(tenantId, null, null, null, Map.of());
    }

    public RequestContext withIdempotencyKey(String key) {
        return new RequestContext(tenantId, correlationId, key, deadline, settings);
    }

    public RequestContext withCorrelationId(String id) {
        return new RequestContext(tenantId, id, idempotencyKey, deadline, settings);
    }

    public RequestContext withDeadline(Deadline value) {
        return new RequestContext(tenantId, correlationId, idempotencyKey, value, settings);
    }

    public RequestContext withSettings(Map<String, ?> values) {
        Map<String, Object> merged = new LinkedHashMap<>(settings);
        merged.putAll(values);
        return new RequestContext(tenantId, correlationId, idempotencyKey, deadline, merged);
    }

    /** A setting of this request; fails, naming it, when absent. */
    @SuppressWarnings("unchecked")
    public <T> T setting(String key) {
        if (!settings.containsKey(key)) {
            throw new NoSuchElementException("'" + key + "' is not a setting of this request");
        }
        return (T) settings.get(key);
    }

    @SuppressWarnings("unchecked")
    public <T> T setting(String key, T fallback) {
        return settings.containsKey(key) ? (T) settings.get(key) : fallback;
    }

    public boolean expired() {
        return deadline != null && deadline.expired();
    }
}
