package com.subhamviky.e2a.identity;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** v1.11.0 (hub design note GCP-01): Cloud Run IAM's Google ID token, and roles granted by configuration. */
class CloudRunIdentityTest {

    static final String O2C = "o2c-app@demo.iam.gserviceaccount.com";

    static String b64(String json) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(json.getBytes(StandardCharsets.UTF_8));
    }

    static String idToken(String email, String issuer) {
        return b64("{\"alg\":\"RS256\"}") + "." + b64("{\"iss\":\"" + issuer + "\",\"aud\":\"https://settlement-abc.a.run.app\","
                + "\"sub\":\"1077\",\"email\":\"" + email + "\",\"exp\":4102444800}") + ".c2ln";
    }

    static IdentityAtIngress service() {
        Map<String, String> env = new HashMap<>(Map.of(
                "E2A_IDENTITY_SOURCE", "gcp_cloud_run",
                "E2A_IDENTITY_TRUSTED_ISSUERS", "[\"https://accounts.google.com\"]",
                "E2A_IDENTITY_TENANTS_FROM_ROLES", "tenant:",
                "E2A_IDENTITY_SUBJECT_ROLES", "{\"" + O2C + "\": [\"tenant:acme\", \"settlement.submit\"]}"));
        return new IdentityAtIngress(IdentitySettings.from(env::get));
    }

    @Test
    void aServiceAccountActsForTheTenantsAndActionsItIsGranted() {
        Identity id = service().resolve(Map.of("Authorization", "Bearer " + idToken(O2C, "https://accounts.google.com")), "acme");
        assertEquals("acme", id.tenantId());
        assertEquals(O2C, id.subject());
        assertEquals(List.of("tenant:acme", "settlement.submit"), id.roles());
        ClaimMapIdentityResolver.requireRoles(id, "settlement.submit");
        assertThrows(IdentityRefused.class, () -> ClaimMapIdentityResolver.requireRoles(id, "settlement.adjust"));
    }

    @Test
    void xServerlessAuthorizationWinsOverAuthorization() {
        Identity id = service().resolve(Map.of(
                "x-serverless-authorization", "Bearer " + idToken(O2C, "https://accounts.google.com"),
                "Authorization", "Bearer " + idToken("someone@else", "https://accounts.google.com")), "acme");
        assertEquals(O2C, id.subject());
    }

    @Test
    void refusals() {
        String token = idToken(O2C, "https://accounts.google.com");
        assertTrue(assertThrows(IdentityRefused.class, () -> service().resolve(Map.of(), "acme")).getMessage().contains("not verified"));
        assertTrue(assertThrows(IdentityRefused.class,
                () -> service().resolve(Map.of("Authorization", "Bearer " + token), "globex")).getMessage().contains("not granted"));
        assertTrue(assertThrows(IdentityRefused.class,
                () -> service().resolve(Map.of("Authorization", "Bearer " + idToken("stranger@x", "https://accounts.google.com")), "acme"))
                .getMessage().contains("not granted"));
        assertTrue(assertThrows(IdentityRefused.class,
                () -> service().resolve(Map.of("Authorization", "Bearer " + idToken(O2C, "https://evil.example")), "acme"))
                .getMessage().contains("not trusted"));
        assertTrue(assertThrows(IdentityRefused.class,
                () -> service().resolve(Map.of("Authorization", "Bearer not-a-jwt"), "acme")).getMessage().contains("malformed"));
    }

    @Test
    void theCloudRunSourceDefaultsToTheGoogleIdTokenPreset() {
        IdentitySettings settings = IdentitySettings.from(Map.of("E2A_IDENTITY_SOURCE", "gcp_cloud_run")::get);
        assertEquals("email", settings.claimMap().get("subject"));
        assertNull(VerifiedClaims.from("gcp_cloud_run", Map.of("Authorization", "Basic dTpw")));
    }
}
