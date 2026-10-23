package com.financeclaw.infrastructure.interaction;

import com.financeclaw.app.interaction.port.InteractionAnswerRules;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.tool.ToolValidator;
import java.util.List;
import org.springframework.stereotype.Component;

/**
 * 实现 InteractionAnswerRules，先用 AgentScope 的 schema 校验人工输入，再运行匹配工具的业务验证器。 校验失败返回稳定的
 * invalid_answer，不回显用户正文。交互归属、审批动作及检查点版本由回答用例和检查点端口先行确认。
 */
@Component
public class AgentScopeAnswerRules implements InteractionAnswerRules {
    private final List<BusinessAnswerValidator> validators;

    public AgentScopeAnswerRules(List<BusinessAnswerValidator> validators) {
        this.validators = List.copyOf(validators);
    }

    @Override
    public void validate(InteractionBinding binding, Checkpoint checkpoint, Answer answer) {
        if (ToolValidator.validateInput(
                        PayloadJson.canonical(answer.answer()),
                        PayloadJson.object(binding.interaction().schema()))
                != null) {
            throw new Problem(422, "invalid_answer");
        }
        for (var validator : validators) {
            if (validator.supports(binding.interaction().tool())) {
                validator.validate(binding, checkpoint, answer.answer());
            }
        }
    }
}
