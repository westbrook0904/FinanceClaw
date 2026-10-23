package com.financeclaw.infrastructure.ziwei;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.ziwei.port.ZiweiPublication;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;
import java.util.Map;
import org.springframework.stereotype.Component;

/**
 * 实现 ZiweiPublication，将计算结果交给统一 Artifact 用例归档。 固定要求 ziwei:read 读取权限；身份、租约、隐私和授权检查由 ArtifactService
 * 的受治理事务完成。
 */
@Component
public class GovernedZiweiPublication implements ZiweiPublication {
    private final ArtifactService artifacts;

    public GovernedZiweiPublication(ArtifactService artifacts) {
        this.artifacts = artifacts;
    }

    @Override
    public Artifact archive(Claim claim, Map<String, Object> artifact) {
        return artifacts.archive(claim, artifact, "ziwei:read");
    }
}
