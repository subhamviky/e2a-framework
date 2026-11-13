package com.subhamviky.e2a.tool;

import com.subhamviky.e2a.core.RequestContext;

import java.util.Map;

/**
 * A tool an agent calls: a read (replay-safe) or a material effect. Agents reach tools only
 * through {@link ReadAccess} and {@link ModifyAccess}, which enforce the kind and the policy.
 */
public interface ToolService {

    /** True for a tool with an effect in a system of record: reached only through io_modify. */
    default boolean material() {
        return false;
    }

    /**
     * Run the tool. {@code idempotencyKey} is set for a material call (always) and null for a
     * read. Return the body; throw for a transport failure (a material call becomes UNKNOWN).
     * A material tool reports its outcome with the body key {@code "outcome"} (a
     * {@link com.subhamviky.e2a.core.PhysicalOutcome} name); absent means COMMITTED.
     */
    Map<String, Object> execute(Map<String, Object> payload, RequestContext ctx, String idempotencyKey) throws Exception;
}
