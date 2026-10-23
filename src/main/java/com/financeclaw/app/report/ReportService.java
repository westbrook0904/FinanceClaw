package com.financeclaw.app.report;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.report.port.ReportGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.support.PayloadJson;
import java.util.Map;
import java.util.UUID;
import org.springframework.stereotype.Service;

/** 报告发布入口：在执行租约与授权校验后原子保存实体和精确回执；重复调用按动作摘要判定回放或冲突。 */
@Service
public class ReportService {
    private final ReportGateway db;
    private final ExecutionLeaseService leases;
    private final ExecutionPolicyService policy;

    public ReportService(
            ReportGateway db, ExecutionLeaseService leases, ExecutionPolicyService policy) {
        this.db = db;
        this.leases = leases;
        this.policy = policy;
    }

    /** 发布本地报告或回放同一工具调用的精确回执；动作摘要不同则拒绝复用键，禁止重复创建业务实体。 */
    public String report(Claim claim, String call, String actionHash, String title) {
        return leases.fenced(
                claim,
                () -> {
                    policy.checkPolicy(claim);
                    // 先验证租约、隐私和授权，再允许回放；撤权后的请求不能借幂等读取绕过治理。
                    var old = db.receipt(claim.turn().id(), call);
                    if (old != null) {
                        if (!old.actionHash().equals(actionHash)) {
                            throw new Blocked("effect_conflict");
                        }
                        return old.result();
                    }
                    // 本地报告没有外部发送窗口，实体与回执由一个 INSERT 共同提交或共同回滚。
                    String id = UUID.randomUUID().toString();
                    String result =
                            PayloadJson.canonical(Map.of("report_id", id, "status", "created"));
                    db.publishReport(id, claim.turn().id(), call, title, actionHash, result);
                    return result;
                });
    }
}
