package com.financeclaw.app.execution.port;

import com.financeclaw.domain.ExecutionPolicy;
import com.financeclaw.domain.Task.Turn;

/** 执行器读取固定发布版本的边界：校验失败即终止，不能回退到当前全局发布。 */
public interface ExecutionRelease {
    String currentHash();

    ExecutionPolicy require(String hash);

    /** 同时校验固定发布与本次执行的 Agent、所有者身份；旧实现仍可只提供发布策略。 */
    default ExecutionPolicy requireForTurn(Turn turn) {
        return require(turn.releaseHash());
    }
}
