package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.*;

/** Agent 运行时边界：只返回检查点关联的执行结果，业务状态和交互恢复由应用层提交。 */
public interface AgentExecutionPort {
    Outcome execute(Claim claim);

    void interrupt(Claim claim);
}
