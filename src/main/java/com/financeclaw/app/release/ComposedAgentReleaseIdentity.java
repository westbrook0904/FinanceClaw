package com.financeclaw.app.release;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.agent.ComposedAgentReleaseManifest;
import com.financeclaw.support.PayloadJson;
import java.util.List;

/** v3 使用独立摘要域，v1/v2 的算法与字段保持不变。 */
public final class ComposedAgentReleaseIdentity {
    private ComposedAgentReleaseIdentity() {}

    public static String hash(ComposedAgentReleaseManifest manifest) {
        return ExecutionScope.hash(
                List.of(ComposedAgentReleaseManifest.FORMAT, PayloadJson.canonical(manifest)));
    }
}
