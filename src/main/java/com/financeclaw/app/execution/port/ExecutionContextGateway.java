package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

/** 执行上下文读取边界：返回当前 Turn 的历史与交互数据，不能跨用户或跨发布版本拼接。 */
public interface ExecutionContextGateway {
    Interaction interaction(String id);

    List<Interaction> interactions(String turn);

    /**
     * 仅供等待通知投影读取当前 pending 交互，避免映射已回答或失效的历史正文。 仍返回完整交互字段及数据库时钟计算的 live，过期 pending 由渲染器继续过滤。
     * 此集合不能代替批次回答完成判断、检查点恢复或 SDK 执行所需的完整交互历史。
     */
    List<Interaction> pendingInteractions(String turn);

    java.time.Instant requestClock(String turn);
}
