package com.financeclaw.infrastructure.governance;

import com.financeclaw.app.governance.port.GrantGateway;
import com.financeclaw.domain.Governance.Grant;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisGrantGateway implements GrantGateway {
  private final GovernanceMapper db;

  public MyBatisGrantGateway(GovernanceMapper db) {
    this.db = db;
  }

  public Grant grant(String conversation) {
    return db.grant(conversation);
  }

  public Grant lockGrant(String conversation) {
    return db.lockGrant(conversation);
  }

  public String grantRequest(String conversation, String key) {
    return db.grantRequest(conversation, key);
  }

  public Grant grantReplay(String conversation, String key) {
    return db.grantReplay(conversation, key);
  }

  public void updateGrant(String conversation, String scopes) {
    db.updateGrant(conversation, scopes);
  }

  public void grantChange(String conversation, String key, String hash) {
    db.grantChange(conversation, key, hash);
  }
}
