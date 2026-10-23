package com.financeclaw.infrastructure.interaction;

import com.financeclaw.app.ziwei.ZiweiInputService;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.infrastructure.ziwei.ZiweiSchemas;
import org.springframework.stereotype.Component;

/**
 * 实现 BusinessAnswerValidator，仅处理固定紫微工具的补充输入。 把真实检查点输入与答案交给
 * ZiweiInputService，沿用工具的字段、时间和授权约束；不根据客户端答案重新构造执行身份或直接调用排盘引擎。
 */
@Component
public class ZiweiAnswerValidator implements BusinessAnswerValidator {
    private final ZiweiInputService inputs;

    public ZiweiAnswerValidator(ZiweiInputService inputs) {
        this.inputs = inputs;
    }

    @Override
    public boolean supports(String tool) {
        return ZiweiSchemas.supports(tool);
    }

    @Override
    public void validate(InteractionBinding binding, Checkpoint checkpoint, Object answer) {
        inputs.validateAnswer(binding.claim(), checkpoint.tool(), checkpoint.input(), answer);
    }
}
