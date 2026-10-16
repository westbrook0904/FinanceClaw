package com.financeclaw.app.release.port;

import com.financeclaw.domain.release.ReleaseDefinition;

public interface ReleaseSource {
  ReleaseDefinition initial();
}
