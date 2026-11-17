package com.subhamviky.e2a.identity;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * E2A's ClaimMapIdentityResolver, in Java, with the same rules: claims must be verified; an
 * issuer outside the trusted list is refused; a user's tenant comes from its claim (through the
 * tenant map) and must equal the one the request names. For a service caller
 * ({@code tenantsFromRoles}, e.g. "tenant:") the tenant is the one the request names, and only
 * if a role "tenant:&lt;id&gt;" grants it (v1.8.0, hub design note SI-01).
 */
public final class ClaimMapIdentityResolver {

    private final IdentitySettings settings;

    public ClaimMapIdentityResolver(IdentitySettings settings) {
        this.settings = settings;
    }

    public Identity resolve(Map<String, Object> claims, boolean verified, String requestTenant) {
        if (!verified || claims == null) {
            throw new IdentityRefused("claims were not verified by the gateway", true);
        }
        Map<String, String> map = settings.claimMap();
        Object issuer = claim(claims, map.getOrDefault("issuer", "iss"));
        if (!settings.trustedIssuers().isEmpty() && (issuer == null || !settings.trustedIssuers().contains(issuer.toString()))) {
            throw new IdentityRefused("issuer " + issuer + " is not trusted", false);
        }
        List<String> roles = roles(claim(claims, map.getOrDefault("roles", "roles")));
        String tenant;
        if (settings.tenantsFromRoles() != null) {
            if (requestTenant == null || requestTenant.isBlank()) {
                throw new IdentityRefused("a service call must name its tenant (X-Tenant-Id)", false);
            }
            if (!roles.contains(settings.tenantsFromRoles() + requestTenant)) {
                throw new IdentityRefused("the caller is not granted tenant '" + requestTenant + "'", false);
            }
            tenant = requestTenant;
        } else {
            Object value = claim(claims, map.getOrDefault("tenant_id", "tenant_id"));
            if (value == null) {
                throw new IdentityRefused("no tenant in the claims (claim " + map.get("tenant_id") + ")", false);
            }
            tenant = settings.tenantMap().getOrDefault(value.toString(), value.toString());
            if (requestTenant != null && !requestTenant.isBlank() && !requestTenant.equals(tenant)) {
                throw new IdentityRefused("the request names another tenant than its credentials", false);
            }
        }
        Object subject = claim(claims, map.getOrDefault("subject", "sub"));
        return new Identity(tenant, subject == null ? null : subject.toString(), roles, issuer == null ? null : issuer.toString());
    }

    /** Refuse (403) unless the identity holds every role, e.g. the action a route performs. */
    public static Identity requireRoles(Identity identity, String... roles) {
        List<String> missing = Arrays.stream(roles).filter(r -> !identity.hasRole(r)).toList();
        if (!missing.isEmpty()) {
            throw new IdentityRefused("the caller lacks the role(s) " + String.join(", ", missing), false);
        }
        return identity;
    }

    /** A claim by name; a dotted path reaches into nested claims. */
    @SuppressWarnings("unchecked")
    static Object claim(Map<String, Object> claims, String path) {
        if (path == null) {
            return null;
        }
        if (claims.containsKey(path)) {
            return claims.get(path);
        }
        Object current = claims;
        for (String part : path.split("\\.")) {
            if (!(current instanceof Map<?, ?> m)) {
                return null;
            }
            current = ((Map<String, Object>) m).get(part);
        }
        return current;
    }

    static List<String> roles(Object value) {
        List<String> out = new ArrayList<>();
        if (value instanceof List<?> list) {
            list.forEach(v -> out.add(String.valueOf(v)));
        } else if (value != null) {
            for (String r : value.toString().replace(',', ' ').split("\\s+")) {
                if (!r.isBlank()) {
                    out.add(r);
                }
            }
        }
        return out;
    }
}
