package com.financeclaw.domain.governance;

import com.financeclaw.domain.Governance.Grant;

/** 权限修改的不可变幂等回执：返回原请求提交的权限版本，重试不能从当前权限反推历史结果。 */
public record GrantReceipt(
        String requestHash, String conversationId, long revision, String scopes) {
    public Grant grant() {
        return new Grant(conversationId, revision, scopes);
    }
}
