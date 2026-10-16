package com.financeclaw.app.execution.port;

import com.financeclaw.domain.ExecutionPolicy;

public interface ExecutionRelease {
  String currentHash();

  ExecutionPolicy require(String hash);
}
