package com.financeclaw.app.agent.port;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.authorization.CredentialResource;
import com.financeclaw.domain.authorization.ResourceGrant;
import java.util.List;

/** HTTP 管理端口；适配器负责原子 CAS 和审计，资源 owner 必须为认证 actor。 */
public interface ResourceAdministration extends AgentAccessGateway {
    void archive(Owner owner, String agentId, long expectedRevision);

    List<CredentialResource> credentialResources(Owner owner);

    ResourceGrant change(
            Owner actor,
            String kind,
            String id,
            Owner grantee,
            String releaseHash,
            long expectedRevision,
            boolean enabled);

    List<ResourceGrant> grants(Owner owner, String kind, String id);

    List<ResourceGrant> history(
            Owner owner, String kind, String id, Owner grantee, long after, int limit);
}
