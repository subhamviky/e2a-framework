package com.subhamviky.e2a.cqrs;

import java.util.Optional;

/**
 * E2A Deterministic CQRS profile, read path (Java realisation of
 * e2a_cqrs_base.BaseQueryService). {@link #fetch} is final: cache-aside around
 * a database read, always scoped to the tenant.
 */
public abstract class BaseQueryService<P, R> {

    public final Optional<R> fetch(P params, String tenantId) {
        String cacheKey = generateCacheKey(params, tenantId);
        Optional<R> cached = readFromCache(cacheKey);
        if (cached.isPresent()) {
            return cached;
        }
        Optional<R> loaded = executeDbRead(params, tenantId);
        loaded.ifPresent(value -> writeToCache(cacheKey, value));
        return loaded;
    }

    protected String generateCacheKey(P params, String tenantId) {
        return tenantId + ":" + params;
    }

    /** No cache by default: reads always see the latest committed state. */
    protected Optional<R> readFromCache(String cacheKey) {
        return Optional.empty();
    }

    protected void writeToCache(String cacheKey, R value) {
    }

    protected abstract Optional<R> executeDbRead(P params, String tenantId);
}
