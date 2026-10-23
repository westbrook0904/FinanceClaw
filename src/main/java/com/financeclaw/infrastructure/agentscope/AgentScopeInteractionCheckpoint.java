package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.app.interaction.port.InteractionCheckpoint;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.ToolCallState;
import org.springframework.stereotype.Repository;

/**
 * 将人工交互绑定到指定执行尝试的 SDK 挂起工具调用。
 *
 * <p>读取时核对 stateVersion、callId、工具名与规范化输入摘要，禁止把旧交互答案 应用到新的检查点。SDK 的参数不一致转换为稳定的
 * checkpoint_mismatch，正文不进入错误响应。
 */
@Repository
public class AgentScopeInteractionCheckpoint implements InteractionCheckpoint {
    private final RuntimeMapper states;

    public AgentScopeInteractionCheckpoint(RuntimeMapper states) {
        this.states = states;
    }

    @Override
    public Checkpoint pending(InteractionBinding binding) {
        var scope = binding.claim().scope();
        var interaction = binding.interaction();
        try {
            var call =
                    ResumeInputs.pending(
                            new MyBatisAgentStateStore(states, scope),
                            scope,
                            interaction.stateVersion(),
                            interaction.callId());
            return new Checkpoint(
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    call.getState() == ToolCallState.ASKING,
                    call.getInput());
        } catch (IllegalArgumentException error) {
            throw new Problem(409, "checkpoint_mismatch");
        }
    }
}
