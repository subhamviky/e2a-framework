package com.subhamviky.e2a.jpa;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.IdClass;
import jakarta.persistence.PostLoad;
import jakarta.persistence.PostPersist;
import jakarta.persistence.Table;
import jakarta.persistence.Transient;
import java.io.Serializable;
import java.time.Instant;
import java.util.Objects;
import org.springframework.data.domain.Persistable;

/**
 * One idempotency claim per (tenant, key). The primary key makes the claim atomic:
 * {@link Persistable#isNew()} forces an INSERT for a new claim (never a merge), so a
 * second claim for the same key fails with a duplicate-key error.
 */
@Entity
@Table(name = "idempotency_record")
@IdClass(IdempotencyRecord.Key.class)
public class IdempotencyRecord implements Persistable<IdempotencyRecord.Key> {

    public enum Status { IN_PROGRESS, SUCCEEDED, FAILED }

    @Id
    @Column(name = "tenant_id")
    private String tenantId;

    @Id
    @Column(name = "idempotency_key")
    private String idempotencyKey;

    @Column(nullable = false)
    private String fingerprint;

    @jakarta.persistence.Enumerated(jakarta.persistence.EnumType.STRING)
    @Column(nullable = false)
    private Status status;

    @Column(name = "http_status")
    private Integer httpStatus;

    @Column(name = "response_body")
    private String responseBody;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @Transient
    private boolean newRecord;

    protected IdempotencyRecord() {
    }

    public IdempotencyRecord(String tenantId, String idempotencyKey, String fingerprint) {
        this.tenantId = tenantId;
        this.idempotencyKey = idempotencyKey;
        this.fingerprint = fingerprint;
        this.status = Status.IN_PROGRESS;
        this.createdAt = Instant.now();
        this.updatedAt = this.createdAt;
        this.newRecord = true;
    }

    @Override
    public Key getId() {
        return new Key(tenantId, idempotencyKey);
    }

    @Override
    public boolean isNew() {
        return newRecord;
    }

    @PostLoad
    @PostPersist
    void markStored() {
        this.newRecord = false;
    }

    public void succeed(int httpStatus, String responseBody) {
        this.status = Status.SUCCEEDED;
        this.httpStatus = httpStatus;
        this.responseBody = responseBody;
        this.updatedAt = Instant.now();
    }

    public void fail() {
        this.status = Status.FAILED;
        this.updatedAt = Instant.now();
    }

    public void reclaim() {
        this.status = Status.IN_PROGRESS;
        this.updatedAt = Instant.now();
    }

    public String getFingerprint() { return fingerprint; }
    public Status getStatus() { return status; }
    public Integer getHttpStatus() { return httpStatus; }
    public String getResponseBody() { return responseBody; }

    /** Composite primary key. */
    public static class Key implements Serializable {
        private String tenantId;
        private String idempotencyKey;

        public Key() {
        }

        public Key(String tenantId, String idempotencyKey) {
            this.tenantId = tenantId;
            this.idempotencyKey = idempotencyKey;
        }

        @Override
        public boolean equals(Object o) {
            return o instanceof Key k && Objects.equals(tenantId, k.tenantId)
                    && Objects.equals(idempotencyKey, k.idempotencyKey);
        }

        @Override
        public int hashCode() {
            return Objects.hash(tenantId, idempotencyKey);
        }
    }
}
