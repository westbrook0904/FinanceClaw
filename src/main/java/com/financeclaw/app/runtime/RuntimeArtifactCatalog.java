package com.financeclaw.app.runtime;

import com.financeclaw.app.runtime.port.RuntimeArtifactGateway;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.runtime.RuntimeArtifact;
import java.util.List;

/** 操作员显式登记本地历史归档；注册不发布版本，也不会启动归档中的代码。 */
public final class RuntimeArtifactCatalog {
    private final RuntimeArtifactGateway db;

    public RuntimeArtifactCatalog(RuntimeArtifactGateway db) {
        this.db = db;
    }

    public RuntimeArtifact register(
            String relativePath, String expectedHash, int protocol, List<String> capabilities) {
        validate(expectedHash, protocol, capabilities);
        var inspected = db.inspect(relativePath, expectedHash, protocol, capabilities);
        db.insert(inspected);
        var stored = db.get(expectedHash);
        if (!inspected.equals(stored))
            throw new Problem(409, "runtime_artifact_registration_conflict");
        return stored;
    }

    public RuntimeArtifact verify(String hash) {
        if (hash == null || !hash.matches("[a-f0-9]{64}"))
            throw new Problem(422, "invalid_artifact_hash");
        var stored = db.get(hash);
        if (stored == null) throw new Problem(404, "runtime_artifact_not_found");
        var observed =
                db.inspect(
                        stored.relativePath(),
                        hash,
                        stored.protocolVersion(),
                        stored.capabilities());
        if (!stored.equals(observed)) throw new Problem(409, "runtime_artifact_changed");
        return stored;
    }

    public List<RuntimeArtifact> list(int limit) {
        if (limit < 1 || limit > 100) throw new Problem(422, "invalid_page_limit");
        return List.copyOf(db.list(limit));
    }

    private static void validate(String hash, int protocol, List<String> capabilities) {
        if (hash == null
                || !hash.matches("[a-f0-9]{64}")
                || protocol != WorkerRegistry.PROTOCOL_VERSION
                || capabilities == null
                || !capabilities.equals(List.of("agentscope")))
            throw new Problem(422, "unsupported_runtime_contract");
    }
}
