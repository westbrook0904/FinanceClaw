package com.financeclaw.app.ziwei.port;

import com.financeclaw.domain.release.ZiweiPolicy;

/** 紫微固定策略读取边界：读取任务发布版本绑定的领域规则和预算，不访问可变运行时缺省值。 */
public interface ZiweiConfiguration {
    ZiweiPolicy policy(String releaseHash);

    /** Returns an execution-owned byte array; the caller clears it after use. */
    byte[] key(String reference);
}
