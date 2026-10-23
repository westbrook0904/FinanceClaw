package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

/** 执行回执与检查点的边界：回执必须匹配当前执行尝试及状态版本，恢复器才能据此收尾。 */
public interface ExecutionReceiptGateway {
    boolean matchesCheckpoint(Claim claim, long version);

    int seal(String attempt, Outcome outcome);

    Receipt receipt(String attempt);

    void insertInteraction(Interaction interaction);
}
