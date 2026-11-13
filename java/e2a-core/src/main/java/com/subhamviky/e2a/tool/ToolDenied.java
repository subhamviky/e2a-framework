package com.subhamviky.e2a.tool;

import com.subhamviky.e2a.core.GovernanceRefusal;

/** A tool call the accessor refused: unknown tool, wrong kind, or not allowed for this agent. */
public class ToolDenied extends GovernanceRefusal {

    public ToolDenied(String reason) {
        super("TOOL_DENIED", "tool_policy", reason, false);
    }
}
