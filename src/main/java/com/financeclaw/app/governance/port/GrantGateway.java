package com.financeclaw.app.governance.port;

import com.financeclaw.domain.Governance.Grant;

/** Durable authorization revisions and the original receipts for grant changes. */
public interface GrantGateway {
  Grant grant(String conversation);

  Grant lockGrant(String conversation);

  String grantRequest(String conversation, String key);

  Grant grantReplay(String conversation, String key);

  void updateGrant(String conversation, String scopes);

  void grantChange(String conversation, String key, String hash);
}
