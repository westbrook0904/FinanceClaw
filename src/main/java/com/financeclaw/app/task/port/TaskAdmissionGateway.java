package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

/** 任务受理的持久化边界：会话锁、受理开关、幂等检查及命令写入加入同一短事务。 */
public interface TaskAdmissionGateway {
    void createConversation(String id, Owner owner);

    String lockConversation(String id, Owner owner);

    Turn replay(String conversation, String key);

    void ensureAdmission(Owner owner);

    boolean admissionPaused(Owner owner);

    boolean active(String conversation);

    void insertTurn(Turn turn);

    void recordAdmission(Turn turn, String command);
}
