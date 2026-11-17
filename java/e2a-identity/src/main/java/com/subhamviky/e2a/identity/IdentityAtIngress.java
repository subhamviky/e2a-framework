package com.subhamviky.e2a.identity;

import java.util.Map;

/**
 * The request's identity at ingress (E2A's identity_at_ingress). Returns null when identity is
 * off ({@code E2A_IDENTITY_SOURCE} unset: local runs and tests take their headers as given).
 * With a source configured it fails closed: no verified claims is 401; an untrusted issuer,
 * no tenant, another tenant, or (for a service caller) a tenant not granted is 403.
 */
public final class IdentityAtIngress {

    private final IdentitySettings settings;
    private final ClaimMapIdentityResolver resolver;

    public IdentityAtIngress(IdentitySettings settings) {
        this.settings = settings;
        this.resolver = new ClaimMapIdentityResolver(settings);
    }

    public static IdentityAtIngress fromEnvironment() {
        return new IdentityAtIngress(IdentitySettings.fromEnvironment());
    }

    public boolean enabled() {
        return settings.enabled();
    }

    public Identity resolve(Map<String, String> headers, String requestTenant) {
        if (!settings.enabled()) {
            return null;
        }
        Map<String, Object> claims;
        try {
            claims = VerifiedClaims.from(settings.source(), headers);
        } catch (IllegalArgumentException malformed) {
            throw new IdentityRefused(malformed.getMessage(), true);
        }
        return resolver.resolve(claims, claims != null, requestTenant);
    }
}
