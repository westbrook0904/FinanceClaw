package com.financeclaw.app.release;

import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Service;

/** 发布配置读取入口：解析并固定执行版本，运行中的任务只能使用受理时绑定的发布身份。 */
@Service
public class ReleaseService {
    private final Map<String, ReleaseDefinition> releases = new ConcurrentHashMap<>();
    private volatile String current;

    public ReleaseService(ReleaseSource source) {
        publish(source.initial());
    }

    public String currentHash() {
        return current;
    }

    public synchronized void publish(ReleaseDefinition release) {
        release.validate();
        String hash = ReleaseIdentity.hash(release);
        releases.putIfAbsent(hash, release);
        current = hash;
    }

    public ReleaseDefinition require(String hash) {
        var release = releases.get(hash);
        if (release == null || !ReleaseIdentity.hash(release).equals(hash)) {
            throw new Blocked("release_unavailable");
        }
        return release;
    }
}
