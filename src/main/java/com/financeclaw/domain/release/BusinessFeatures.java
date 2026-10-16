package com.financeclaw.domain.release;

import java.util.*;

public record BusinessFeatures(
    List<RemoteEndpoint> remotes, String skillHash, String marketJson, ZiweiPolicy ziwei) {
  public BusinessFeatures(List<RemoteEndpoint> remotes, String skillHash, String marketJson) {
    this(remotes, skillHash, marketJson, null);
  }

  public BusinessFeatures {
    remotes = List.copyOf(remotes);
  }
}
