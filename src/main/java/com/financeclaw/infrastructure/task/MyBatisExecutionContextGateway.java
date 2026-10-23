package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import java.util.List;
import org.springframework.stereotype.Repository;

/**
 * 实现 ExecutionContextGateway，为运行适配器读取人工交互和 Turn 的受理时间。 这些内部读取不代替外部 owner
 * 校验或交互锁，恢复调用方仍须验证检查点、授权和有效期。相对时间计算使用已持久化的创建时间，避免重试跨日改变请求含义。
 */
@Repository
public class MyBatisExecutionContextGateway implements ExecutionContextGateway {
    private final GovernanceMapper db;

    public MyBatisExecutionContextGateway(GovernanceMapper db) {
        this.db = db;
    }

    @Override
    public Interaction interaction(String id) {
        return db.getInteractionById(id);
    }

    @Override
    public List<Interaction> interactions(String turn) {
        return db.listInteractionsByTurnId(turn);
    }

    @Override
    public java.time.Instant requestClock(String turn) {
        return db.getTurnCreatedAt(turn);
    }
}
