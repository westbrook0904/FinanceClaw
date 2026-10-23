package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.EmbeddingProfiles;
import org.springframework.stereotype.Component;

@Component
public class MyBatisEmbeddingProfiles implements EmbeddingProfiles {
  private final MemoryMapper db;

  public MyBatisEmbeddingProfiles(MemoryMapper db) {
    this.db = db;
  }

  public void registerProfile(String revision, String fingerprint, String model) {
    db.insertEmbeddingProfileIfAbsent(revision, fingerprint, model);
  }

  public String profile(String revision) {
    return db.getEmbeddingProfileFingerprint(revision);
  }
}
