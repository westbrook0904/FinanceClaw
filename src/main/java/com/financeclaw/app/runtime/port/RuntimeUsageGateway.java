package com.financeclaw.app.runtime.port;

import com.financeclaw.domain.runtime.RuntimeUsage;

public interface RuntimeUsageGateway {
    RuntimeUsage snapshot();
}
