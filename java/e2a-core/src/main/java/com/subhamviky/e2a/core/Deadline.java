package com.subhamviky.e2a.core;

import java.time.Duration;

/** The point in time a request must finish by. Every wait is bounded by what remains of it. */
public record Deadline(long expiresAtNanos) {

    public static Deadline after(Duration duration) {
        return new Deadline(System.nanoTime() + duration.toNanos());
    }

    public long remainingMillis() {
        return Math.max(0, (expiresAtNanos - System.nanoTime()) / 1_000_000);
    }

    public boolean expired() {
        return System.nanoTime() >= expiresAtNanos;
    }
}
