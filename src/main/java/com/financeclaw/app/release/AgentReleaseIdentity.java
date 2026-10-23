package com.financeclaw.app.release;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.support.PayloadJson;
import java.util.List;

/** Agent 新格式使用独立摘要域；旧 ReleaseIdentity 与既有正文不参与格式改写。 */
public final class AgentReleaseIdentity {
    private AgentReleaseIdentity() {}

    public static String hash(AgentReleaseManifest manifest) {
        return ExecutionScope.hash(
                List.of(AgentReleaseManifest.FORMAT, PayloadJson.canonical(manifest)));
    }
}
