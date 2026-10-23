package com.financeclaw.app.ziwei.port;

import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;
import java.util.Map;

/** 紫微结果发布边界：确定性计算与有副作用发布分开处理，发布必须通过受治理的产物接口。 */
public interface ZiweiPublication {
    Artifact archive(Claim claim, Map<String, Object> artifact);
}
