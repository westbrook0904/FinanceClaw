package com.financeclaw.app.ziwei;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.support.*;
import com.financeclaw.ziwei.*;
import java.util.List;

/** 紫微请求的确定性身份：规范化输入与领域版本后计算摘要，防止相同步骤编号被用于不同输入。 */
public final class ZiweiIdentity {
    private ZiweiIdentity() {}

    public static String binding(byte[] key, ZiweiPolicy policy, String turn) {
        return PayloadDigest.hmacSha256(
                key,
                PayloadJson.canonical(List.of("ziwei-key-binding", policy.keyVersion(), turn)));
    }

    public static String fingerprint(
            ZiweiInputs.Normalized input, Owner owner, ZiweiPolicy policy, byte[] key) {
        return PayloadDigest.hmacSha256(
                key,
                PayloadJson.canonical(
                        List.of(
                                owner.tenant(),
                                owner.subject(),
                                input.birthJson(),
                                input.birth().solarDate().toString(),
                                input.time().shichen(),
                                input.timezone(),
                                policy.rules(),
                                policy.keyVersion())));
    }
}
