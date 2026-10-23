package com.financeclaw.infrastructure.governance;

import com.financeclaw.app.report.port.ReportGateway;
import com.financeclaw.domain.report.ReportReceipt;
import org.springframework.stereotype.Repository;

/**
 * 实现 ReportGateway，读取精确报告回执并以单条 INSERT 发布完整报告。 要求数据库只插入一行，唯一键阻止重复发布；动作摘要校验、租约和授权检查在 ReportService
 * 的外层事务完成。此处没有远程发送或半成品报告状态。
 */
@Repository
public class MyBatisReportGateway implements ReportGateway {
    private final GovernanceMapper db;

    public MyBatisReportGateway(GovernanceMapper db) {
        this.db = db;
    }

    @Override
    public ReportReceipt receipt(String turn, String call) {
        return db.getReportReceipt(turn, call);
    }

    @Override
    public void publishReport(
            String id, String turn, String call, String title, String hash, String result) {
        if (db.insertReportPublication(id, turn, call, title, hash, result) != 1) {
            throw new IllegalStateException("Report publication did not insert exactly one row");
        }
    }
}
