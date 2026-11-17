package com.subhamviky.e2a.identity;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * The identity settings, with the same names and environment variables as E2A (Python):
 * E2A_IDENTITY_SOURCE, E2A_IDENTITY_CLAIM_MAP, E2A_IDENTITY_TRUSTED_ISSUERS,
 * E2A_IDENTITY_TENANT_MAP and E2A_IDENTITY_TENANTS_FROM_ROLES. {@code source} null means
 * identity is off (local runs and tests): the service takes its headers as given.
 */
public record IdentitySettings(String source, Map<String, String> claimMap, List<String> trustedIssuers,
                               Map<String, String> tenantMap, String tenantsFromRoles) {

    public static final Map<String, Map<String, String>> PRESETS = Map.of(
            "entra_id", Map.of("tenant_id", "tid", "subject", "oid", "roles", "roles", "issuer", "iss"),
            "entra_service", Map.of("tenant_id", "tid", "subject", "azp", "roles", "roles", "issuer", "iss"));

    private static final ObjectMapper JSON = new ObjectMapper();

    public IdentitySettings {
        claimMap = claimMap == null ? Map.of() : Map.copyOf(claimMap);
        trustedIssuers = trustedIssuers == null ? List.of() : List.copyOf(trustedIssuers);
        tenantMap = tenantMap == null ? Map.of() : Map.copyOf(tenantMap);
        source = source == null || source.isBlank() ? null : source;
        tenantsFromRoles = tenantsFromRoles == null || tenantsFromRoles.isBlank() ? null : tenantsFromRoles;
    }

    public static IdentitySettings off() {
        return new IdentitySettings(null, null, null, null, null);
    }

    public boolean enabled() {
        return source != null;
    }

    /** From the environment (or any lookup, for tests). Without a claim map, a service
     *  configuration (tenants from roles) uses the entra_service preset, otherwise entra_id. */
    public static IdentitySettings from(Function<String, String> env) {
        String prefix = env.apply("E2A_IDENTITY_TENANTS_FROM_ROLES");
        Map<String, String> map = readMap(env.apply("E2A_IDENTITY_CLAIM_MAP"));
        if (map.isEmpty()) {
            map = PRESETS.get(prefix == null || prefix.isBlank() ? "entra_id" : "entra_service");
        }
        return new IdentitySettings(env.apply("E2A_IDENTITY_SOURCE"), map, readList(env.apply("E2A_IDENTITY_TRUSTED_ISSUERS")),
                readMap(env.apply("E2A_IDENTITY_TENANT_MAP")), prefix);
    }

    public static IdentitySettings fromEnvironment() {
        return from(System::getenv);
    }

    private static Map<String, String> readMap(String text) {
        try {
            return text == null || text.isBlank() ? Map.of() : JSON.readValue(text, new TypeReference<Map<String, String>>() { });
        } catch (Exception e) {
            throw new IllegalArgumentException("not a JSON object: " + text, e);
        }
    }

    private static List<String> readList(String text) {
        try {
            return text == null || text.isBlank() ? List.of() : JSON.readValue(text, new TypeReference<List<String>>() { });
        } catch (Exception e) {
            throw new IllegalArgumentException("not a JSON list: " + text, e);
        }
    }
}
