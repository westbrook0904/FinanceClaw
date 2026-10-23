package com.financeclaw.app.governance.port;

import com.financeclaw.domain.Governance.Grant;
import com.financeclaw.domain.governance.GrantReceipt;

/** 会话授权存储边界：当前权限和不可变修改回执分开读取，避免重试将当前状态误当原始结果。 */
public interface GrantGateway {
    Grant grant(String conversation);

    Grant lockGrant(String conversation);

    GrantReceipt grantReceipt(String conversation, String key);

    void updateGrant(String conversation, String scopes);

    void grantChange(String conversation, String key, String hash);
}
