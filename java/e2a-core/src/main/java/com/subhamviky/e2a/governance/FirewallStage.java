package com.subhamviky.e2a.governance;

import com.subhamviky.e2a.core.GovernanceRefusal;
import com.subhamviky.e2a.core.RequestContext;

import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/** Screens the named state fields: injection patterns and a maximum length. */
public final class FirewallStage implements Stage {

    private final List<Pattern> injectionPatterns;
    private final int maxLength;
    private final List<String> fields;

    public FirewallStage(List<String> injectionPatterns, int maxLength, List<String> fields) {
        this.injectionPatterns = injectionPatterns.stream().map(p -> Pattern.compile(p, Pattern.CASE_INSENSITIVE)).toList();
        this.maxLength = maxLength;
        this.fields = List.copyOf(fields);
    }

    @Override
    public void check(Map<String, Object> state, RequestContext ctx) {
        for (String field : fields) {
            Object value = state.get(field);
            if (value == null) {
                continue;
            }
            String text = value.toString();
            if (text.length() > maxLength) {
                throw new GovernanceRefusal("FIREWALL", "firewall", field + " is longer than " + maxLength, false);
            }
            for (Pattern p : injectionPatterns) {
                if (p.matcher(text).find()) {
                    throw new GovernanceRefusal("FIREWALL", "firewall", field + " matches a blocked pattern", false);
                }
            }
        }
    }
}
