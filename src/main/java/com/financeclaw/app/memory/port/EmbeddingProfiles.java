package com.financeclaw.app.memory.port;

public interface EmbeddingProfiles {
  void registerProfile(String revision, String fingerprint, String model);

  String profile(String revision);
}
