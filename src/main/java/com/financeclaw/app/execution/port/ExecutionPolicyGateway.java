package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

/** 授权与调用预算的持久化边界：权限行锁串行撤权和执行检查，计数更新必须具有原子上限条件。 */
public interface ExecutionPolicyGateway {
    void ensureGrant(String conversation, String scopes);

    Grant lockGrant(String conversation);

    void insertPolicy(String turn, long revision, String scopes);

    Policy policy(String turn);

    int modelCall(String turn, int max);

    int toolCall(String turn, int max);

    void invalidate(String turn);

    String startedAt(String attempt);
}
