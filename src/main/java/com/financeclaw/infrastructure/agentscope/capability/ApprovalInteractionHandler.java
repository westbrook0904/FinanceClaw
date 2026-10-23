package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import io.agentscope.core.message.*;
import java.util.function.Function;

/**
 * 将 SDK 中处于 ASKING 的工具调用转换为待审批交互。 审批绑定原
 * callId、工具名和规范化动作摘要，问题文案由注册工具提供；其他状态显式拒绝。该处理器不执行批准后的业务写入，答案和恢复仍走统一检查点流程。
 */
public record ApprovalInteractionHandler(Function<ToolUseBlock, String> question)
        implements InteractionHandler {
    @Override
    public boolean approval() {
        return true;
    }

    @Override
    public Pending pending(ToolUseBlock call) {
        if (call.getState() != ToolCallState.ASKING) {
            throw new Blocked("unsupported_interaction");
        }
        return new Pending(
                "approval",
                call.getId(),
                call.getName(),
                ResumeInputs.actionHash(call),
                "{}",
                question.apply(call));
    }
}
