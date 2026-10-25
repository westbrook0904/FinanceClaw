package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;
import org.springframework.stereotype.Repository;

/**
 * 实现 CommandGateway，委托命令领取、租约续期、attempt 创建与过期候选查询。 领取与状态迁移的事务、command 到 turn 的锁顺序由执行用例协调。续期保留可空
 * Boolean：未命中租约与成功续期后的取消状态不能混为一个值。
 */
@Repository
public class MyBatisCommandGateway implements CommandGateway {
    private final TaskMapper db;

    public MyBatisCommandGateway(TaskMapper db) {
        this.db = db;
    }

    @Override
    public Command nextCommand() {
        return db.getNextQueuedCommandForUpdateSkipLocked();
    }

    @Override
    public Command lockCommand(String id) {
        return db.getCommandForUpdate(id);
    }

    @Override
    public Command lockTurnCommand(String turn) {
        return db.getCommandByTurnIdForUpdate(turn);
    }

    @Override
    public Command claim(String id, String attempt, String worker, long lease) {
        return db.updateCommandClaim(id, attempt, worker, lease);
    }

    @Override
    public void insertAttempt(Command command) {
        db.insertAttempt(command);
    }

    @Override
    public Boolean renew(Claim claim, long lease) {
        return db.updateCommandLeaseAndGetCancellation(claim, lease);
    }

    @Override
    public void finishCommand(String command) {
        db.updateCommandDone(command);
    }

    @Override
    public List<String> expired() {
        return db.listExpiredCommandIds();
    }
}
