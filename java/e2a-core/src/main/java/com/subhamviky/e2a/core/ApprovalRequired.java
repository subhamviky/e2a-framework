package com.subhamviky.e2a.core;

/** A human must approve before the step goes on. It pauses the step; it is not an error. */
public class ApprovalRequired extends GovernanceRefusal {

    public ApprovalRequired(String reason) {
        super("APPROVAL_REQUIRED", "approval", reason, true);
    }
}
