package com.subhamviky.e2a.jpa;

import com.subhamviky.e2a.core.IdempotencyStore;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.AutoConfigurationPackage;
import org.springframework.boot.autoconfigure.condition.ConditionalOnClass;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.orm.jpa.HibernateJpaAutoConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * Registers the idempotency entity and repository and a {@link JpaIdempotencyStore} bean.
 * {@code @AutoConfigurationPackage} adds this package to the packages Spring Boot scans for
 * entities and repositories, next to the application's own (an {@code @EntityScan} here would
 * replace them). It runs before the JPA auto-configuration so that the repository scan sees
 * this package. An application that defines its own {@link IdempotencyStore} keeps it.
 */
@AutoConfiguration(before = HibernateJpaAutoConfiguration.class)
@AutoConfigurationPackage
@ConditionalOnClass(name = "org.springframework.data.jpa.repository.JpaRepository")
public class E2AJpaAutoConfiguration {

    @Bean
    @ConditionalOnMissingBean(IdempotencyStore.class)
    public JpaIdempotencyStore e2aIdempotencyStore(IdempotencyRecordRepository repository,
                                                  PlatformTransactionManager txManager) {
        return new JpaIdempotencyStore(repository, txManager);
    }
}
