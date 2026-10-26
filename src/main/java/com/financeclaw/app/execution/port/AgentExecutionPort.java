package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.*;
import java.util.function.BooleanSupplier;

/** Agent 运行时边界：只返回检查点关联的执行结果，业务状态和交互恢复由应用层提交。 */
public interface AgentExecutionPort {
    Outcome execute(Claim claim);

    /** Local attempt lifetime is additional to the authoritative database execution fence. */
    default Outcome execute(Claim claim, BooleanSupplier allowed) {
        if (!allowed.getAsBoolean()) throw new Fenced();
        return execute(claim);
    }

    void interrupt(Claim claim);
}
