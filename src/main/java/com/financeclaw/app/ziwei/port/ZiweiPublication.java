package com.financeclaw.app.ziwei.port;

import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;
import java.util.Map;

public interface ZiweiPublication {
  Artifact archive(Claim claim, Map<String, Object> artifact);
}
