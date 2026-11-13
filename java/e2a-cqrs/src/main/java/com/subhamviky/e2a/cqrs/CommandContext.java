package com.subhamviky.e2a.cqrs;

/**
 * Request-scoped execution context (E2A propagation fields), passed explicitly
 * to every command and query instead of living on long-lived beans.
 */
public record CommandContext(String tenantId, String idempotencyKey, String correlationId) {
}
