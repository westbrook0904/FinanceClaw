package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

/** 执行上下文读取边界：返回当前 Turn 的历史与交互数据，不能跨用户或跨发布版本拼接。 */
public interface ExecutionContextGateway {
    Interaction interaction(String id);

    List<Interaction> interactions(String turn);

    java.time.Instant requestClock(String turn);
}
