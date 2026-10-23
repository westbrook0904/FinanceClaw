package com.financeclaw.app.interaction.port;

import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;

/** 工具交互内容校验边界：按检查点中固定的输入契约验证回答，不能改用可变工具定义。 */
public interface InteractionAnswerRules {
    void validate(InteractionBinding binding, Checkpoint checkpoint, Answer answer);
}
