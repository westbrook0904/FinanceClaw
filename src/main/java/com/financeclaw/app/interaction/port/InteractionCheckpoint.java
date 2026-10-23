package com.financeclaw.app.interaction.port;

import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;

/** 交互检查点读取边界：仅返回与当前执行尝试绑定的待回答状态，不执行 Agent 或修改业务授权。 */
public interface InteractionCheckpoint {
    Checkpoint pending(InteractionBinding binding);
}
