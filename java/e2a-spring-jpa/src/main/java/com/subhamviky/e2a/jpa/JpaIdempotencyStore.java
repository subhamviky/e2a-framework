package com.subhamviky.e2a.jpa;

import com.subhamviky.e2a.core.Claim;
import com.subhamviky.e2a.core.ClaimStatus;
import com.subhamviky.e2a.core.IdempotencyStore;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.Optional;

/**
 * E2A's {@link IdempotencyStore} on a database table ({@code idempotency_record}, see
 * {@code com/subhamviky/e2a/jpa/schema.sql}): the INSERT of the (tenant, key) row is the atomic
 * claim, so two concurrent callers can never both execute the same request. Each call runs in
 * its own transaction, independent of the caller's.
 */
public class JpaIdempotencyStore implements IdempotencyStore {

    private final IdempotencyRecordRepository repository;
    private final TransactionTemplate tx;

    public JpaIdempotencyStore(IdempotencyRecordRepository repository, PlatformTransactionManager txManager) {
        this.repository = repository;
        this.tx = new TransactionTemplate(txManager);
        this.tx.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    }

    @Override
    public Claim claim(String tenantId, String key, String fingerprint) {
        IdempotencyRecord.Key id = new IdempotencyRecord.Key(tenantId, key);
        try {
            tx.executeWithoutResult(s -> repository.saveAndFlush(new IdempotencyRecord(tenantId, key, fingerprint)));
            return Claim.of(ClaimStatus.CLAIMED);
        } catch (DataIntegrityViolationException alreadyThere) {
            // fall through: someone claimed this key before us
        }
        return tx.execute(s -> {
            Optional<IdempotencyRecord> found = repository.findById(id);
            if (found.isEmpty()) {
                return Claim.of(ClaimStatus.IN_PROGRESS);
            }
            IdempotencyRecord existing = found.get();
            if (!existing.getFingerprint().equals(fingerprint)) {
                return Claim.of(ClaimStatus.CONFLICT);
            }
            return switch (existing.getStatus()) {
                case SUCCEEDED -> new Claim(ClaimStatus.REPLAY,
                        new Claim.StoredResult(existing.getHttpStatus(), existing.getResponseBody()));
                case IN_PROGRESS -> Claim.of(ClaimStatus.IN_PROGRESS);
                case FAILED -> {
                    existing.reclaim();                 // a retryable failure may be claimed again
                    repository.save(existing);
                    yield Claim.of(ClaimStatus.CLAIMED);
                }
            };
        });
    }

    @Override
    public void complete(String tenantId, String key, int status, String body) {
        tx.executeWithoutResult(s -> repository.findById(new IdempotencyRecord.Key(tenantId, key)).ifPresent(r -> {
            r.succeed(status, body);
            repository.save(r);
        }));
    }

    @Override
    public void fail(String tenantId, String key) {
        tx.executeWithoutResult(s -> repository.findById(new IdempotencyRecord.Key(tenantId, key)).ifPresent(r -> {
            r.fail();
            repository.save(r);
        }));
    }
}
