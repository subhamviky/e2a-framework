package com.subhamviky.e2a.cqrs;

import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validator;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * E2A Deterministic CQRS profile, write path (Java realisation of
 * e2a_cqrs_base.BaseCommandService). {@link #mutate} is the single public
 * entry point and is final; subclasses specialise only the protected hooks.
 */
public abstract class BaseCommandService<I> {

    private static final Logger logger = LoggerFactory.getLogger(BaseCommandService.class);

    private final Validator validator;
    private final long maxLatencyMillis;

    protected BaseCommandService(Validator validator, long maxLatencyMillis) {
        this.validator = validator;
        this.maxLatencyMillis = maxLatencyMillis;
    }

    /** Template method: validate, run the business logic, commit, publish, sync the cache. */
    public final CqrsResult mutate(I input, CommandContext context) {
        long start = System.nanoTime();
        Set<ConstraintViolation<I>> violations = validator.validate(input);
        if (!violations.isEmpty()) {
            List<String> errors = violations.stream()
                    .sorted(Comparator.comparing(v -> v.getPropertyPath().toString()))
                    .map(v -> v.getPropertyPath() + " " + v.getMessage())
                    .toList();
            return CqrsResult.of(400, Map.of("errors", errors));
        }
        CqrsResult result = executeBusinessLogic(input, context);
        commitVersioned(result, context);
        executeOutboxCommit(result, context);
        synchronizeCache(result, context);
        long millis = (System.nanoTime() - start) / 1_000_000;
        if (millis > maxLatencyMillis) {
            logger.warn("command latency SLO breached: {} ms > {} ms", millis, maxLatencyMillis);
        }
        return result;
    }

    /** The domain step. Must keep every material state change inside its own transactions. */
    protected abstract CqrsResult executeBusinessLogic(I input, CommandContext context);

    /**
     * Conditional commit. Here the JPA {@code @Version} columns enforce optimistic
     * concurrency when the business transaction commits, so the default is a no-op.
     */
    protected void commitVersioned(CqrsResult result, CommandContext context) {
    }

    /**
     * Outbox publication. A store that commits the aggregate and the outbox row in
     * one transaction folds this into the business logic (see SettlementSaga).
     */
    protected void executeOutboxCommit(CqrsResult result, CommandContext context) {
    }

    /** Read-model cache invalidation. No cache in this service, so a no-op. */
    protected void synchronizeCache(CqrsResult result, CommandContext context) {
    }
}
