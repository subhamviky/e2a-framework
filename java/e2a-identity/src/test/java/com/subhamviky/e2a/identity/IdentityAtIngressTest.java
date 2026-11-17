package com.subhamviky.e2a.identity;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;

/** The same cases as E2A's Python identity tests (v1.7.0, v1.7.4, v1.8.0). */
class IdentityAtIngressTest {

    static String principal(List<String> roles, String appId) {
        String claims = roles.stream().map(r -> "{\"typ\":\"roles\",\"val\":\"" + r + "\"}").collect(Collectors.joining(","));
        String json = "{\"auth_typ\":\"aad\",\"role_typ\":\"roles\",\"claims\":["
                + "{\"typ\":\"iss\",\"val\":\"https://login.microsoftonline.com/dir/v2.0\"},"
                + "{\"typ\":\"azp\",\"val\":\"" + appId + "\"},"
                + "{\"typ\":\"http://schemas.microsoft.com/identity/claims/tenantid\",\"val\":\"dir\"}"
                + (claims.isEmpty() ? "" : "," + claims) + "]}";
        return Base64.getEncoder().encodeToString(json.getBytes(StandardCharsets.UTF_8));
    }

    static IdentityAtIngress service(String... extra) {
        Map<String, String> env = new HashMap<>(Map.of("E2A_IDENTITY_SOURCE", "azure_container_apps",
                "E2A_IDENTITY_TENANTS_FROM_ROLES", "tenant:"));
        for (int i = 0; i + 1 < extra.length; i += 2) {
            env.put(extra[i], extra[i + 1]);
        }
        return new IdentityAtIngress(IdentitySettings.from(env::get));
    }

    static Map<String, String> headers(String principal) {
        return Map.of("x-ms-client-principal", principal);
    }

    @Test
    void offMeansLocalMode() {
        assertNull(new IdentityAtIngress(IdentitySettings.from(k -> null)).resolve(Map.of(), "acme"));
    }

    @Test
    void aServiceActsForAGrantedTenant() {
        Identity id = service().resolve(headers(principal(List.of("settlement.submit", "tenant:acme"), "o2c-app")), "acme");
        assertEquals("acme", id.tenantId());
        assertEquals("o2c-app", id.subject());
        assertSame(id, ClaimMapIdentityResolver.requireRoles(id, "settlement.submit"));
    }

    @Test
    void noPrincipalIs401() {
        IdentityRefused r = assertThrows(IdentityRefused.class, () -> service().resolve(Map.of(), "acme"));
        assertEquals(401, r.httpStatus());
        assertEquals("IDENTITY", r.code());
    }

    @Test
    void malformedPrincipalIs401() {
        assertEquals(401, assertThrows(IdentityRefused.class,
                () -> service().resolve(headers("not-base64!"), "acme")).httpStatus());
    }

    @Test
    void aTenantNotGrantedOrNoneIs403() {
        String p = principal(List.of("tenant:acme"), "o2c-app");
        assertEquals(403, assertThrows(IdentityRefused.class, () -> service().resolve(headers(p), "globex")).httpStatus());
        assertEquals(403, assertThrows(IdentityRefused.class, () -> service().resolve(headers(p), null)).httpStatus());
    }

    @Test
    void aMissingActionRoleIs403() {
        Identity id = service().resolve(headers(principal(List.of("tenant:acme"), "recon-app")), "acme");
        IdentityRefused r = assertThrows(IdentityRefused.class, () -> ClaimMapIdentityResolver.requireRoles(id, "settlement.adjust"));
        assertTrue(r.reason().contains("settlement.adjust"));
        assertEquals(403, r.httpStatus());
    }

    @Test
    void anUntrustedIssuerIs403() {
        String p = principal(List.of("tenant:acme"), "o2c-app");
        assertThrows(IdentityRefused.class, () -> service("E2A_IDENTITY_TRUSTED_ISSUERS", "[\"https://other\"]").resolve(headers(p), "acme"));
        assertEquals("acme", service("E2A_IDENTITY_TRUSTED_ISSUERS", "[\"https://login.microsoftonline.com/dir/v2.0\"]")
                .resolve(headers(p), "acme").tenantId());
    }

    @Test
    void usersResolveTheirTenantFromTheirClaimsAsBefore() {
        IdentityAtIngress users = new IdentityAtIngress(IdentitySettings.from(Map.of(
                "E2A_IDENTITY_SOURCE", "azure_container_apps", "E2A_IDENTITY_TENANT_MAP", "{\"dir\":\"acme\"}")::get));
        String p = principal(List.of("approver"), "app");
        assertEquals("acme", users.resolve(headers(p), "acme").tenantId());
        assertEquals("acme", users.resolve(headers(p), null).tenantId());
        assertThrows(IdentityRefused.class, () -> users.resolve(headers(p), "globex"));
    }
}
