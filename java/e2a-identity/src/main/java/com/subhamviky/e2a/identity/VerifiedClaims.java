package com.subhamviky.e2a.identity;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Verified claims as the platform hands them to the service (E2A's e2a_adapters.ingress).
 * {@code azure_container_apps}: Container Apps (and App Service) authentication's
 * X-MS-CLIENT-PRINCIPAL header, base64 JSON {claims: [{typ, val}], role_typ, auth_typ}. External
 * requests cannot set that header, so its presence means the platform verified the token.
 * {@code gcp_cloud_run} (v1.11.0, GCP-01): the Google ID token Cloud Run IAM verified before the
 * request reached the container, from X-Serverless-Authorization or else Authorization: Bearer;
 * its payload is read, not re-verified. Trustworthy only while the service is not public.
 */
public final class VerifiedClaims {

    public static final String AZURE_PRINCIPAL_HEADER = "X-MS-CLIENT-PRINCIPAL";
    public static final String AZURE_CONTAINER_APPS = "azure_container_apps";
    public static final String GCP_CLOUD_RUN = "gcp_cloud_run";
    public static final String SERVERLESS_AUTHORIZATION_HEADER = "X-Serverless-Authorization";

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Map<String, String> AZURE_ALIASES = Map.of(
            "http://schemas.microsoft.com/identity/claims/tenantid", "tid",
            "http://schemas.microsoft.com/identity/claims/objectidentifier", "oid",
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier", "sub",
            "http://schemas.microsoft.com/ws/2008/06/identity/claims/role", "roles");

    private VerifiedClaims() {
    }

    /** The claims, or null when the platform's carrier is absent (not verified). */
    public static Map<String, Object> from(String source, Map<String, String> headers) {
        if (GCP_CLOUD_RUN.equals(source)) {
            return cloudRun(headers);
        }
        if (!AZURE_CONTAINER_APPS.equals(source)) {
            throw new IllegalArgumentException("identity source " + source
                    + " is not supported by e2a-java: use azure_container_apps or gcp_cloud_run");
        }
        String value = header(headers, AZURE_PRINCIPAL_HEADER);
        return value == null || value.isBlank() ? null : azure(value);
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> cloudRun(Map<String, String> headers) {
        String value = header(headers, SERVERLESS_AUTHORIZATION_HEADER);
        if (value == null || value.isBlank()) {
            value = header(headers, "Authorization");
        }
        if (value == null || value.isBlank()) {
            return null;
        }
        String[] scheme = value.trim().split("\\s+", 2);
        if (scheme.length != 2 || !"bearer".equalsIgnoreCase(scheme[0]) || scheme[1].isBlank()) {
            return null;
        }
        String[] parts = scheme[1].trim().split("\\.");
        if (parts.length != 3) {
            throw new IllegalArgumentException("malformed gcp_cloud_run identity carrier: the bearer token is not a JWT");
        }
        Map<String, Object> claims;
        try {
            claims = JSON.readValue(Base64.getUrlDecoder().decode(parts[1]), Map.class);
        } catch (Exception e) {
            throw new IllegalArgumentException("malformed gcp_cloud_run identity carrier: " + e.getMessage(), e);
        }
        if (claims == null || claims.get("iss") == null || (claims.get("email") == null && claims.get("sub") == null)) {
            throw new IllegalArgumentException("malformed gcp_cloud_run identity carrier: the token names no issuer or subject");
        }
        return new LinkedHashMap<>(claims);
    }

    static String header(Map<String, String> headers, String name) {
        if (headers == null) {
            return null;
        }
        for (Map.Entry<String, String> e : headers.entrySet()) {
            if (e.getKey() != null && e.getKey().equalsIgnoreCase(name)) {
                return e.getValue();
            }
        }
        return null;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> azure(String value) {
        JsonNode principal;
        try {
            principal = JSON.readTree(new String(Base64.getDecoder().decode(value.trim()), StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalArgumentException("malformed " + AZURE_PRINCIPAL_HEADER, e);
        }
        if (principal == null || !principal.isObject()) {
            throw new IllegalArgumentException(AZURE_PRINCIPAL_HEADER + " does not hold a JSON object");
        }
        String roleTyp = principal.path("role_typ").asText(null);
        Map<String, Object> claims = new LinkedHashMap<>();
        for (JsonNode item : principal.path("claims")) {
            String typ = item.path("typ").asText(null);
            String val = item.path("val").asText(null);
            if (typ == null) {
                continue;
            }
            List<String> names = new ArrayList<>(List.of(typ));
            String alias = AZURE_ALIASES.get(typ);
            if (alias != null && !names.contains(alias)) {
                names.add(alias);
            }
            if (roleTyp != null && typ.equals(roleTyp) && !names.contains("roles")) {
                names.add("roles");
            }
            for (String name : names) {
                if ("roles".equals(name)) {
                    ((List<Object>) claims.computeIfAbsent(name, k -> new ArrayList<>())).add(val);
                } else if (claims.containsKey(name)) {
                    Object existing = claims.get(name);
                    List<Object> list = existing instanceof List<?> l ? new ArrayList<>(l) : new ArrayList<>(List.of(existing));
                    list.add(val);
                    claims.put(name, list);
                } else {
                    claims.put(name, val);
                }
            }
        }
        if (principal.hasNonNull("auth_typ")) {
            claims.putIfAbsent("idp", principal.get("auth_typ").asText());
        }
        return claims;
    }
}
