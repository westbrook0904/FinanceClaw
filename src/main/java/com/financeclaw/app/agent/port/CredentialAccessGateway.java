package com.financeclaw.app.agent.port;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.release.ReleaseDefinition;

/** 验证服务端凭据资源使用权，不返回密钥，不将资源owner替换为执行caller。 */
public interface CredentialAccessGateway {
    void requireModel(Owner caller);

    void requireCredential(Owner caller, String reference);

    default void requireRelease(Owner caller, ReleaseDefinition definition) {
        if (!definition.primary().name().equals("offline")
                || (definition.fallback() != null
                        && !definition.fallback().name().equals("offline"))) requireModel(caller);
        for (var remote : definition.business().remotes())
            requireCredential(caller, remote.credentialRef());
    }

    /** 只供既有显式装配fixture；生产必须使用服务端目录的实现。 */
    static CredentialAccessGateway trustedFixture() {
        return new CredentialAccessGateway() {
            public void requireModel(Owner owner) {}

            public void requireCredential(Owner owner, String reference) {}
        };
    }
}
