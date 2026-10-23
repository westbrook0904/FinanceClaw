package com.financeclaw.infrastructure.governance;

import com.financeclaw.app.governance.port.GrantGateway;
import com.financeclaw.domain.Governance.Grant;
import com.financeclaw.domain.governance.GrantReceipt;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisGrantGateway implements GrantGateway {
  private final GovernanceMapper db;

  public MyBatisGrantGateway(GovernanceMapper db) {
    this.db = db;
  }

  public Grant grant(String conversation) {
    return db.getGrant(conversation);
  }

  public Grant lockGrant(String conversation) {
    return db.getGrantForUpdate(conversation);
  }

  public GrantReceipt grantReceipt(String conversation, String key) {
    return db.getGrantReceipt(conversation, key);
  }

  public void updateGrant(String conversation, String scopes) {
    db.updateGrant(conversation, scopes);
  }

  public void grantChange(String conversation, String key, String hash) {
    db.insertGrantChange(conversation, key, hash);
  }
}
