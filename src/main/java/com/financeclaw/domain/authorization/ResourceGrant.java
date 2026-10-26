package com.financeclaw.domain.authorization;

import com.financeclaw.domain.Task.Owner;

public record ResourceGrant(
        String resourceKind,
        String resourceId,
        String resourceTenant,
        String resourceSubject,
        String granteeTenant,
        String granteeSubject,
        String releaseHash,
        String bindingHash,
        long revision,
        boolean enabled) {
    public Owner resourceOwner() {
        return new Owner(resourceTenant, resourceSubject);
    }

    public Owner grantee() {
        return new Owner(granteeTenant, granteeSubject);
    }
}
