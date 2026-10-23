package com.financeclaw.app.report.port;

import com.financeclaw.domain.report.ReportReceipt;

/** 报告实体与发布回执的存储边界；所有写入都加入调用方的执行栅栏事务。 */
public interface ReportGateway {
    /** 根据稳定的 Turn 与工具调用标识读取已提交回执，不从标题或实体编号重建结果。 */
    ReportReceipt receipt(String turn, String call);

    /** 单次写入报告及精确回执；调用方必须先持有当前执行命令的排他锁。 */
    void publishReport(
            String id, String turn, String call, String title, String hash, String result);
}
