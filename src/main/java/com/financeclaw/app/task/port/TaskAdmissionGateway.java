package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

/** 任务受理的持久化边界：会话锁、受理开关、幂等检查及命令写入加入同一短事务。 */
public interface TaskAdmissionGateway {
    void createConversation(String id, Owner owner);

    String lockConversation(String id, Owner owner);

    /** 仅在持有已验证 owner 的会话锁时读取；null 表示尚未使用的历史会话。 */
    String conversationRelease(String id);

    /** 只允许首次绑定，返回 0 表示已有绑定；与会话创建或首次受理共同提交。 */
    int bindConversationRelease(String id, String releaseHash);

    Turn replay(String conversation, String key);

    void ensureAdmission(Owner owner);

    boolean admissionPaused(Owner owner);

    boolean active(String conversation);

    void insertTurn(Turn turn);

    void recordAdmission(Turn turn, String command);
}
