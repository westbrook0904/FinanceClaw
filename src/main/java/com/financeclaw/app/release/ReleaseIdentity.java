package com.financeclaw.app.release;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.PayloadJson;
import java.util.List;

public final class ReleaseIdentity {
  private ReleaseIdentity() {}

  public static String hash(ReleaseDefinition release) {
    return ExecutionScope.hash(List.of("agentscope-2.0.3", PayloadJson.canonical(release)));
  }
}
