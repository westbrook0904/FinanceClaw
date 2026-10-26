package com.financeclaw.domain.execution;

import java.util.List;

/**
 * 根 Turn 内固定研究或私有叶子 Agent 子调用的持久身份和累计预算。
 *
 * <p>owner、发布、隐私和授权版本来自根 Turn 的受理记录；结果复用 capability_steps 回执， 不在子调用行中保存正文或第二份结果。 resultHash 表示最终
 * result 步骤绑定的输入摘要。
 */
public record ChildInvocation(
        String rootTurnId,
        String callId,
        String target,
        String targetAgentId,
        String targetReleaseHash,
        String inputHash,
        List<String> scopes,
        String status,
        int modelCalls,
        int toolCalls,
        String tenant,
        String subject,
        String conversationId,
        String releaseHash,
        long epoch,
        long grantRevision,
        String resultHash,
        String result,
        String leafProfile) {
    public ChildInvocation(
            String rootTurnId,
            String callId,
            String target,
            String targetAgentId,
            String targetReleaseHash,
            String inputHash,
            List<String> scopes,
            String status,
            int modelCalls,
            int toolCalls,
            String tenant,
            String subject,
            String conversationId,
            String releaseHash,
            long epoch,
            long grantRevision,
            String resultHash,
            String result) {
        this(
                rootTurnId,
                callId,
                target,
                targetAgentId,
                targetReleaseHash,
                inputHash,
                scopes,
                status,
                modelCalls,
                toolCalls,
                tenant,
                subject,
                conversationId,
                releaseHash,
                epoch,
                grantRevision,
                resultHash,
                result,
                "legacy");
    }

    public ChildInvocation {
        scopes = List.copyOf(scopes);
    }
}
