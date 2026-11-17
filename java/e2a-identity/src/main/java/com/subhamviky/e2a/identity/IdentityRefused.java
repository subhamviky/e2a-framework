package com.subhamviky.e2a.identity;

import com.subhamviky.e2a.core.GovernanceRefusal;

/** The request's identity is refused (code IDENTITY), as E2A's {@code IdentityRefused}. */
public class IdentityRefused extends GovernanceRefusal {
    /** True when the request carried no verified identity at all (HTTP 401); otherwise 403. */
    private final boolean unauthenticated;

    public IdentityRefused(String reason, boolean unauthenticated) {
        super("IDENTITY", "identity", reason, false);
        this.unauthenticated = unauthenticated;
    }

    public boolean unauthenticated() {
        return unauthenticated;
    }

    public int httpStatus() {
        return unauthenticated ? 401 : 403;
    }
}
