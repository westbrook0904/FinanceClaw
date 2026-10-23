package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.StepResult;
import com.financeclaw.domain.Task.*;

/** 确定性能力步骤的存储边界：同一 Turn、命名空间及步骤键只保存一个输入摘要和结果。 */
public interface CapabilityStepGateway {
    StepResult step(String turn, String namespace, String step);

    void saveStep(String turn, String namespace, String step, String hash, String result);
}
