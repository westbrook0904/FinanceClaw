package com.financeclaw.app.release;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.PayloadJson;
import java.util.List;

/** 发布身份计算：将运行所依赖的模型、工具及业务配置归入确定性摘要，避免恢复时静默切换执行语义。 */
public final class ReleaseIdentity {
    private ReleaseIdentity() {}

    public static String hash(ReleaseDefinition release) {
        return ExecutionScope.hash(List.of("agentscope-2.0.3", PayloadJson.canonical(release)));
    }
}
