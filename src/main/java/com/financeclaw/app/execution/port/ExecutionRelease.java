package com.financeclaw.app.execution.port;

import com.financeclaw.domain.ExecutionPolicy;

/** 执行器读取固定发布版本的边界：校验失败即终止，不能回退到当前全局发布。 */
public interface ExecutionRelease {
    String currentHash();

    ExecutionPolicy require(String hash);
}
