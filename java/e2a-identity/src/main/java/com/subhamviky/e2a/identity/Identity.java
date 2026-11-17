package com.subhamviky.e2a.identity;

import java.util.List;

/** The caller of a request, from claims the platform verified (E2A's {@code Identity}). */
public record Identity(String tenantId, String subject, List<String> roles, String issuer) {
    public Identity {
        roles = roles == null ? List.of() : List.copyOf(roles);
    }

    public boolean hasRole(String role) {
        return roles.contains(role);
    }
}
