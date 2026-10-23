package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

/** 任务状态及会话记录的写入边界：调用方持有任务和命令锁，状态事件与业务变更共同提交。 */
public interface TaskMutationGateway {
    Turn lockTurn(String id);

    Accepted cancelReplay(String turn, String key);

    void recordCancel(String turn, String key, String status, long revision);

    void transition(String turn, String status, String result, String reason);

    void event(String turn);

    void journal(String conversation, String turn, String role, String content);
}
