package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Governance.Pending;
import io.agentscope.core.message.Msg;
import io.agentscope.core.message.ToolUseBlock;

/** A tool owns the question it asks and how its validated answer resumes that tool. */
public interface InteractionHandler {
    Pending pending(ToolUseBlock call);

    default boolean approval() {
        return false;
    }

    default Msg resume(ToolUseBlock call, Object answer) {
        throw new Blocked("unsupported_interaction");
    }
}
