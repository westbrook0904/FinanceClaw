package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

/** 持久命令和租约的边界：领取、续租及关闭必须参与调用方事务，执行 fencing 由数据库决定。 */
public interface CommandGateway {
    Command nextCommand();

    Command lockCommand(String id);

    Command lockTurnCommand(String turn);

    /** 调用方已按 command → Turn 加锁；返回更新后的真实行，null 必须令领取事务整体失败。 */
    Command claim(String id, String attempt, String worker, long lease);

    void insertAttempt(Command command);

    /** 返回空表示执行已经失去租约；非空值是取得任务锁之后观察到的取消状态。 */
    Boolean renew(Claim claim, long lease);

    void finishCommand(String command);

    List<String> expired();
}
