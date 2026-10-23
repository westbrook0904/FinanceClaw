package com.financeclaw.app.operations.port;

import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import java.util.Map;

/** 用户范围的只读运维边界：只返回状态和计数，禁止暴露消息、画像正文、工具参数及凭据。 */
public interface OperationsReadGateway {
    record Overview(
            Map<String, Object> tasks,
            Map<String, Object> calls,
            Map<String, Object> notifications) {}

    record DrainSnapshot(Admission admission, Map<String, Long> counts) {}

    Overview overview(Owner owner);

    /** Admission and every count must be observed in one database snapshot. */
    DrainSnapshot drain(Owner owner);

    List<TurnBacklog> turns(Owner owner, String after, int limit);

    List<NotificationBacklog> notifications(Owner owner, String after, int limit);

    List<BindingBacklog> bindings(Owner owner, String after, int limit);

    List<MemoryBacklog> memoryJobs(Owner owner, long after, int limit);
}
