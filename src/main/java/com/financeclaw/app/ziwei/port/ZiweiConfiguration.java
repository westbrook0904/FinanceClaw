package com.financeclaw.app.ziwei.port;

import com.financeclaw.domain.release.ZiweiPolicy;

public interface ZiweiConfiguration {
  ZiweiPolicy policy(String releaseHash);

  /** Returns an execution-owned byte array; the caller clears it after use. */
  byte[] key(String reference);
}
