-- The table JpaIdempotencyStore uses (PostgreSQL / H2). Put it in your own migrations
-- (Flyway, Liquibase); e2a-java does not run migrations for you.
create table idempotency_record (
    tenant_id       varchar(40)   not null,
    idempotency_key varchar(100)  not null,
    fingerprint     varchar(64)   not null,
    status          varchar(12)   not null,
    http_status     integer,
    response_body   varchar(4000),
    created_at      timestamp with time zone not null,
    updated_at      timestamp with time zone not null,
    primary key (tenant_id, idempotency_key)
);
