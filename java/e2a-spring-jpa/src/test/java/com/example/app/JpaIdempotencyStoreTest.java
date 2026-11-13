package com.example.app;

import com.subhamviky.e2a.core.Claim;
import com.subhamviky.e2a.core.ClaimStatus;
import com.subhamviky.e2a.core.IdempotencyStore;
import com.subhamviky.e2a.jpa.JpaIdempotencyStore;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.stream.IntStream;

import static org.junit.jupiter.api.Assertions.*;

@SpringBootTest(classes = TestApp.class)
class JpaIdempotencyStoreTest {

    @Autowired
    IdempotencyStore store;

    @Autowired
    AccountRepository accounts;

    @Test
    void theAutoConfigurationProvidesTheStoreAndKeepsTheApplicationsOwnEntities() {
        assertInstanceOf(JpaIdempotencyStore.class, store);
        Account a = new Account();
        a.id = "A1";
        accounts.save(a);
        assertTrue(accounts.findById("A1").isPresent());
    }

    @Test
    void claimCompleteReplayConflictAndRetryAfterFailure() {
        assertEquals(ClaimStatus.CLAIMED, store.claim("t", "k1", "f").status());
        assertEquals(ClaimStatus.IN_PROGRESS, store.claim("t", "k1", "f").status());
        store.complete("t", "k1", 201, "{\"ok\":true}");
        Claim replay = store.claim("t", "k1", "f");
        assertEquals(ClaimStatus.REPLAY, replay.status());
        assertEquals(201, replay.stored().status());
        assertEquals("{\"ok\":true}", replay.stored().body());
        assertEquals(ClaimStatus.CONFLICT, store.claim("t", "k1", "other").status());
        assertEquals(ClaimStatus.CLAIMED, store.claim("other-tenant", "k1", "x").status(), "keys are tenant-scoped");
        assertEquals(ClaimStatus.CLAIMED, store.claim("t", "k2", "f").status());
        store.fail("t", "k2");
        assertEquals(ClaimStatus.CLAIMED, store.claim("t", "k2", "f").status(), "a failed key may be claimed again");
    }

    @Test
    void concurrentClaimsHaveExactlyOneWinner() throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(12);
        CountDownLatch go = new CountDownLatch(1);
        List<Callable<ClaimStatus>> calls = IntStream.range(0, 24).<Callable<ClaimStatus>>mapToObj(i -> () -> {
            go.await();
            return store.claim("t", "race", "f").status();
        }).toList();
        List<Future<ClaimStatus>> futures = calls.stream().map(pool::submit).toList();
        go.countDown();
        long winners = 0;
        for (Future<ClaimStatus> f : futures) {
            winners += f.get() == ClaimStatus.CLAIMED ? 1 : 0;
        }
        pool.shutdown();
        assertEquals(1, winners);
    }
}
