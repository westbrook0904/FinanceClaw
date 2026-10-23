package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.CapabilityStepGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.StepResult;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import org.springframework.stereotype.Repository;

/**
 * 实现 CapabilityStepGateway，保存本地确定性步骤的输入摘要与结果文本。 按 turn、namespace 和 step 定位步骤，重放与输入冲突判断由
 * CapabilityStepService 执行；此表不提供外部副作用的未知结果对账。写入加入调用方的执行栅栏事务。
 */
@Repository
public class MyBatisCapabilityStepGateway implements CapabilityStepGateway {
    private final GovernanceMapper db;

    public MyBatisCapabilityStepGateway(GovernanceMapper db) {
        this.db = db;
    }

    @Override
    public StepResult step(String turn, String namespace, String step) {
        var saved = db.getCapabilityStep(turn, namespace, step);
        return saved == null ? null : new StepResult(saved.inputHash(), saved.result());
    }

    @Override
    public void saveStep(String turn, String namespace, String step, String hash, String result) {
        db.insertCapabilityStep(turn, namespace, step, hash, result);
    }
}
