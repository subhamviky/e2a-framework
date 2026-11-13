package com.subhamviky.e2a.core;

import java.util.LinkedHashMap;
import java.util.Map;

/** A typed refusal: a governance stage (or a tool policy) refused the request, with a reason. */
public class GovernanceRefusal extends RuntimeException {

    private final String code;
    private final String stage;
    private final boolean retryable;

    public GovernanceRefusal(String code, String stage, String reason, boolean retryable) {
        super(reason);
        this.code = code;
        this.stage = stage;
        this.retryable = retryable;
    }

    public String code() {
        return code;
    }

    public String stage() {
        return stage;
    }

    public String reason() {
        return getMessage();
    }

    public boolean retryable() {
        return retryable;
    }

    /** The form recorded in {@code state.refusal}. */
    public Map<String, Object> toMap() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("code", code);
        out.put("stage", stage);
        out.put("reason", reason());
        out.put("retryable", retryable);
        return out;
    }
}
