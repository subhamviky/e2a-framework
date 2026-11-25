package com.subhamviky.e2a.identity;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * The identity settings, with the same names and environment variables as E2A (Python):
 * E2A_IDENTITY_SOURCE, E2A_IDENTITY_CLAIM_MAP, E2A_IDENTITY_TRUSTED_ISSUERS,
 * E2A_IDENTITY_TENANT_MAP, E2A_IDENTITY_TENANTS_FROM_ROLES and (v1.11.0) E2A_IDENTITY_SUBJECT_ROLES,
 * roles granted by configuration to a verified subject. {@code source} null means identity is off
 * (local runs and tests): the service takes its headers as given.
 */
public record IdentitySettings(String source, Map<String, String> claimMap, List<String> trustedIssuers,
                               Map<String, String> tenantMap, String tenantsFromRoles,
                               Map<String, List<String>> subjectRoles) {

    public static final Map<String, Map<String, String>> PRESETS = Map.of(
            "entra_id", Map.of("tenant_id", "tid", "subject", "oid", "roles", "roles", "issuer", "iss"),
            "entra_service", Map.of("tenant_id", "tid", "subject", "azp", "roles", "roles", "issuer", "iss"),
            // v1.11.0 (GCP-01): a Google ID token verified by Cloud Run IAM; the subject is the email
            "google_id_token", Map.of("tenant_id", "tenant_id", "subject", "email", "roles", "roles", "issuer", "iss"));

    private static final ObjectMapper JSON = new ObjectMapper();

    public IdentitySettings {
        claimMap = claimMap == null ? Map.of() : Map.copyOf(claimMap);
        trustedIssuers = trustedIssuers == null ? List.of() : List.copyOf(trustedIssuers);
        tenantMap = tenantMap == null ? Map.of() : Map.copyOf(tenantMap);
        source = source == null || source.isBlank() ? null : source;
        tenantsFromRoles = tenantsFromRoles == null || tenantsFromRoles.isBlank() ? null : tenantsFromRoles;
        subjectRoles = subjectRoles == null ? Map.of() : Map.copyOf(subjectRoles);
    }

    /** The settings before v1.11.0: no roles granted by configuration. */
    public IdentitySettings(String source, Map<String, String> claimMap, List<String> trustedIssuers,
                            Map<String, String> tenantMap, String tenantsFromRoles) {
        this(source, claimMap, trustedIssuers, tenantMap, tenantsFromRoles, null);
    }

    public static IdentitySettings off() {
        return new IdentitySettings(null, null, null, null, null, null);
    }

    public boolean enabled() {
        return source != null;
    }

    /** From the environment (or any lookup, for tests). Without a claim map, the source
     *  gcp_cloud_run uses the google_id_token preset; otherwise a service configuration (tenants
     *  from roles) uses entra_service, and anything else entra_id. */
    public static IdentitySettings from(Function<String, String> env) {
        String source = env.apply("E2A_IDENTITY_SOURCE");
        String prefix = env.apply("E2A_IDENTITY_TENANTS_FROM_ROLES");
        Map<String, String> map = readMap(env.apply("E2A_IDENTITY_CLAIM_MAP"));
        if (map.isEmpty()) {
            map = PRESETS.get(VerifiedClaims.GCP_CLOUD_RUN.equals(source) ? "google_id_token"
                    : prefix == null || prefix.isBlank() ? "entra_id" : "entra_service");
        }
        return new IdentitySettings(source, map, readList(env.apply("E2A_IDENTITY_TRUSTED_ISSUERS")),
                readMap(env.apply("E2A_IDENTITY_TENANT_MAP")), prefix, readRoles(env.apply("E2A_IDENTITY_SUBJECT_ROLES")));
    }

    /** The roles configuration grants a subject (none when it names none). */
    public List<String> rolesOf(String subject) {
        return subject == null ? List.of() : subjectRoles.getOrDefault(subject, List.of());
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

    private static Map<String, List<String>> readRoles(String text) {
        try {
            return text == null || text.isBlank() ? Map.of()
                    : JSON.readValue(text, new TypeReference<Map<String, List<String>>>() { });
        } catch (Exception e) {
            throw new IllegalArgumentException("not a JSON object of role lists: " + text, e);
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
