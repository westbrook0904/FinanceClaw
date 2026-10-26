package com.financeclaw.app.runtime.port;

import com.financeclaw.domain.runtime.RuntimeWorker;
import java.util.List;

public interface WorkerRegistryGateway {
    void register(String id, String hash, int parallelism, long ttlMillis);

    boolean heartbeat(String id, String hash, int active, long ttlMillis);

    RuntimeWorker get(String id);

    List<RuntimeWorker> list(int limit);

    void stop(String id);
}
