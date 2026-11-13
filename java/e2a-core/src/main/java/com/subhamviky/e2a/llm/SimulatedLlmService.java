package com.subhamviky.e2a.llm;

import com.subhamviky.e2a.core.RequestContext;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.function.Predicate;

/**
 * A deterministic stand-in for a model, for tests and local runs: the first rule whose
 * condition matches the conversation answers. No rule matches: the default answer.
 */
public final class SimulatedLlmService implements LlmService {

    private record Rule(String name, Predicate<String> when, Function<String, String> answer) {
    }

    private final List<Rule> rules = new ArrayList<>();
    private final String fallback;
    private final List<String> seen = new ArrayList<>();

    public SimulatedLlmService(String fallback) {
        this.fallback = fallback;
    }

    /** Answer with {@code answer} when the user's text contains {@code contains}. */
    public SimulatedLlmService when(String contains, String answer) {
        rules.add(new Rule(contains, t -> t.contains(contains), t -> answer));
        return this;
    }

    public SimulatedLlmService rule(String name, Predicate<String> when, Function<String, String> answer) {
        rules.add(new Rule(name, when, answer));
        return this;
    }

    /** The rule names that answered, in order. */
    public synchronized List<String> seen() {
        return List.copyOf(seen);
    }

    @Override
    public synchronized LlmResponse infer(List<Map<String, String>> messages, RequestContext ctx) {
        String text = String.join("\n", messages.stream().map(m -> m.getOrDefault("content", "")).toList());
        for (Rule rule : rules) {
            if (rule.when().test(text)) {
                seen.add(rule.name());
                return new LlmResponse(rule.answer().apply(text), text.length() / 4);
            }
        }
        seen.add("default");
        return new LlmResponse(fallback, text.length() / 4);
    }
}
