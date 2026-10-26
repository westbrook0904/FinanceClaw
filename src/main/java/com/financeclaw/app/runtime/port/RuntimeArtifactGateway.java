package com.financeclaw.app.runtime.port;

import com.financeclaw.domain.runtime.RuntimeArtifact;
import java.util.List;

public interface RuntimeArtifactGateway {
    RuntimeArtifact inspect(
            String relativePath, String expectedHash, int protocol, List<String> capabilities);

    void insert(RuntimeArtifact artifact);

    RuntimeArtifact get(String hash);

    List<RuntimeArtifact> list(int limit);
}
