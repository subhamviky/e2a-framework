package com.subhamviky.e2a.cqrs;

import java.util.Map;

/** Outcome of a command or query: HTTP status, body, and whether it was replayed from the idempotency store. */
public record CqrsResult(int httpStatus, Object body, boolean replayed) {

    public static CqrsResult of(int httpStatus, Object body) {
        return new CqrsResult(httpStatus, body, false);
    }

    public static CqrsResult error(int httpStatus, String message) {
        return new CqrsResult(httpStatus, Map.of("error", message), false);
    }
}
