package com.subhamviky.e2a.workflow;

import com.subhamviky.e2a.agent.BaseAgent;

import java.util.Map;
import java.util.function.Predicate;

/** One declared step: a {@link Step} or a {@link Route}. */
public sealed interface WorkflowStep permits Step, Route {

    String name();

    BaseAgent agentFor(Map<String, Object> state);

    /** Runs the step only when true; otherwise SKIPPED. Null = always. */
    Predicate<Map<String, Object>> when();

    /** After the step: true ends the workflow here. Null = never. */
    Predicate<Map<String, Object>> stopIf();

    /** The idempotency claim is taken before the first material step. */
    boolean material();
}
