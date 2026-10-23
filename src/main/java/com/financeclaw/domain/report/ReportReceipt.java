package com.financeclaw.domain.report;

import java.util.Objects;

/** 报告发布的不可变回执；只有实体与结果在同一事务提交后，才允许按原始结果回放。 */
public record ReportReceipt(String actionHash, String result) {
    public ReportReceipt {
        Objects.requireNonNull(actionHash, "报告回执必须绑定动作摘要");
        Objects.requireNonNull(result, "报告回执必须包含已提交的结果");
    }
}
