package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.Turn;
import java.util.List;

/** 执行器访问记忆的边界：受理绑定版本、执行检查隔离状态，历史批量过滤必须由实现提供，避免默认逐条查询造成数据库热点。 */
public interface ExecutionMemory {
    void admit(Turn turn);

    void check(Turn turn);

    boolean sameEpoch(Turn turn);

    List<Turn> filterHistory(Turn current, List<Turn> history);

    void completed(String turn);

    String context(Turn turn);
}
