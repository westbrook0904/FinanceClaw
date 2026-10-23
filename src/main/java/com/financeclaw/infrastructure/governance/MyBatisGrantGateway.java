package com.financeclaw.infrastructure.governance;

import com.financeclaw.app.governance.port.GrantGateway;
import com.financeclaw.domain.Governance.Grant;
import com.financeclaw.domain.governance.GrantReceipt;
import org.springframework.stereotype.Repository;

/**
 * 实现 GrantGateway，连接可变会话授权与不可变变更回执。 授权修改由用例先加排他锁，版本、状态及回执共同提交；幂等读取一次返回当时的请求摘要和授权快照。回放旧请求不会改读最新
 * grant 冒充原结果。
 */
@Repository
public class MyBatisGrantGateway implements GrantGateway {
    private final GovernanceMapper db;

    public MyBatisGrantGateway(GovernanceMapper db) {
        this.db = db;
    }

    @Override
    public Grant grant(String conversation) {
        return db.getGrant(conversation);
    }

    @Override
    public Grant lockGrant(String conversation) {
        return db.getGrantForUpdate(conversation);
    }

    @Override
    public GrantReceipt grantReceipt(String conversation, String key) {
        return db.getGrantReceipt(conversation, key);
    }

    @Override
    public void updateGrant(String conversation, String scopes) {
        db.updateGrant(conversation, scopes);
    }

    @Override
    public void grantChange(String conversation, String key, String hash) {
        db.insertGrantChange(conversation, key, hash);
    }
}
