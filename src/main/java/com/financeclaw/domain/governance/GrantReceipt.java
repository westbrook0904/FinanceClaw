package com.financeclaw.domain.governance;

import com.financeclaw.domain.Governance.Grant;

/** Immutable grant-change receipt, including its request identity in the same read. */
public record GrantReceipt(
    String requestHash, String conversationId, long revision, String scopes) {
  public Grant grant() {
    return new Grant(conversationId, revision, scopes);
  }
}
