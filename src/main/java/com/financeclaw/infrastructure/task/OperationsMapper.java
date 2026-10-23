package com.financeclaw.infrastructure.task;

import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import java.util.Map;
import org.apache.ibatis.annotations.*;

/**
 * 按 owner 读取当前任务、交互、记忆和飞书通知积压。
 *
 * <p>查询只输出运行事实，不返回用户消息、工具参数或通知正文。排空摘要在单条 SQL 的同一 MVCC 快照内计算；它是观察结果，不会阻止新的请求，暂停受理由独立控制操作完成。
 */
@Mapper
public interface OperationsMapper {
    // Match the existing native-channel projection queue, without exposing its payload or binding
    // identity.
    String UNPROJECTED =
            """
            NOT b.muted AND t.status IN ('waiting','blocked','completed','failed','cancelled')
            AND NOT EXISTS(SELECT 1 FROM channel.notices n
              WHERE n.turn_id=t.id AND n.revision=t.revision AND n.status<>'suppressed')
            """;

    /** 按 owner 与稳定 ID 游标分页，只读取当前未完成任务或待处理交互，不连接报告正文。 */
    @Select(
            """
WITH facts AS (
  SELECT t.id,t.conversation_id,t.status,t.revision,t.reason,c.status AS command_status,
    (SELECT count(*) FROM business.interactions i WHERE i.turn_id=t.id AND i.status='pending') AS pending_interactions
  FROM business.turns t LEFT JOIN business.commands c ON c.turn_id=t.id
  WHERE t.tenant=#{owner.tenant} AND t.subject=#{owner.subject} AND t.id>#{after}
)
SELECT * FROM facts WHERE status NOT IN ('completed','failed','cancelled') OR command_status<>'done'
  OR pending_interactions>0
ORDER BY id LIMIT #{limit}
""")
    List<TurnBacklog> listTurnBacklog(
            @Param("owner") Owner owner, @Param("after") String after, @Param("limit") int limit);

    @Select(
            """
SELECT n.id,t.conversation_id,n.turn_id,n.status,n.revision
FROM channel.notices n JOIN channel.bindings b ON b.id=n.binding_id
JOIN business.turns t ON t.id=n.turn_id
WHERE b.tenant=#{owner.tenant} AND b.subject=#{owner.subject}
  AND t.tenant=#{owner.tenant} AND t.subject=#{owner.subject}
  AND n.status IN ('pending','sending','unknown','submitted_unverified') AND n.id>#{after}
ORDER BY n.id LIMIT #{limit}
""")
    List<NotificationBacklog> listNotificationBacklog(
            @Param("owner") Owner owner, @Param("after") String after, @Param("limit") int limit);

    @Select(
            """
            SELECT b.id,t.conversation_id,t.id AS turn_id,t.status,t.revision
            FROM channel.bindings b JOIN business.turns t ON t.id=b.active_turn
            WHERE b.tenant=#{owner.tenant} AND b.subject=#{owner.subject}
              AND t.tenant=#{owner.tenant} AND t.subject=#{owner.subject} AND b.id>#{after} AND
            """
                    + UNPROJECTED
                    + " ORDER BY b.id LIMIT #{limit}")
    List<BindingBacklog> listBindingBacklog(
            @Param("owner") Owner owner, @Param("after") String after, @Param("limit") int limit);

    @Select(
            """
            SELECT id::text,kind,status,epoch,revision,attempts,error_code FROM memory.jobs
            WHERE tenant=#{owner.tenant} AND subject=#{owner.subject}
              AND status NOT IN ('done','obsolete','forgotten') AND id>#{after}
            ORDER BY memory.jobs.id LIMIT #{limit}
            """)
    List<MemoryBacklog> listMemoryJobBacklog(
            @Param("owner") Owner owner, @Param("after") long after, @Param("limit") int limit);

    /** 在一条 SQL 内读取受理状态和全部积压计数，避免不同语句快照拼出不真实的已排空结果。 */
    @Select(
            """
WITH owned AS MATERIALIZED (
  SELECT id,status,revision FROM business.turns WHERE tenant=#{tenant} AND subject=#{subject}
)
SELECT
  COALESCE((SELECT paused FROM business.admission WHERE tenant=#{tenant} AND subject=#{subject}),false) AS paused,
  COALESCE((SELECT revision FROM business.admission WHERE tenant=#{tenant} AND subject=#{subject}),0) AS revision,
  count(*) FILTER(WHERE status IN ('queued','resuming')) AS queued,
  count(*) FILTER(WHERE status='running') AS running,
  count(*) FILTER(WHERE status='waiting') AS waiting,
  count(*) FILTER(WHERE status='cancelling') AS cancelling,
  count(*) FILTER(WHERE status='blocked') AS blocked,
  (SELECT count(*) FROM business.commands WHERE turn_id IN (SELECT id FROM owned) AND status<>'done') AS active_commands,
  (SELECT count(*) FROM business.interactions WHERE turn_id IN (SELECT id FROM owned) AND status='pending') AS pending_interactions,
  (SELECT count(*) FROM channel.notices n JOIN channel.bindings b ON b.id=n.binding_id
    JOIN owned t ON t.id=n.turn_id
    WHERE b.tenant=#{tenant} AND b.subject=#{subject} AND n.status IN ('pending','sending')) AS pending_notifications,
  (SELECT count(*) FROM channel.notices n JOIN channel.bindings b ON b.id=n.binding_id
    JOIN owned t ON t.id=n.turn_id
    WHERE b.tenant=#{tenant} AND b.subject=#{subject} AND n.status IN ('unknown','submitted_unverified')) AS unverified_notifications,
  (SELECT count(*) FROM channel.bindings b JOIN owned t ON t.id=b.active_turn
    WHERE b.tenant=#{tenant} AND b.subject=#{subject} AND
"""
                    + UNPROJECTED
                    + """
  ) AS unprojected_notifications,
  (SELECT count(*) FROM memory.jobs WHERE tenant=#{tenant} AND subject=#{subject}
    AND status NOT IN ('done','obsolete','forgotten')) AS pending_memory_jobs,
  (SELECT count(*) FROM memory.owners WHERE tenant=#{tenant} AND subject=#{subject} AND purge_pending) AS pending_forget
FROM owned
""")
    Map<String, Object> getDrainSummary(Owner owner);

    @Select(
            """
            SELECT count(*) AS turns,
              count(*) FILTER(WHERE status='completed') AS completed,
              count(*) FILTER(WHERE status='failed') AS failed,
              count(*) FILTER(WHERE status='blocked') AS blocked,
              count(*) FILTER(WHERE status IN ('queued','resuming')) AS queued,
              count(*) FILTER(WHERE status='running') AS running,
              count(*) FILTER(WHERE status='waiting') AS waiting,
              COALESCE(max(EXTRACT(EPOCH FROM clock_timestamp()-created_at)*1000)
                FILTER(WHERE status IN ('queued','resuming')),0) AS oldest_queued_ms
            FROM business.turns WHERE tenant=#{tenant} AND subject=#{subject}
            """)
    Map<String, Object> getTaskStatistics(Owner owner);

    @Select(
            """
SELECT COALESCE(sum(p.model_calls),0) AS model_calls, COALESCE(sum(p.tool_calls),0) AS tool_calls
FROM business.turn_policies p JOIN business.turns t ON t.id=p.turn_id
WHERE t.tenant=#{tenant} AND t.subject=#{subject}
""")
    Map<String, Object> getCallStatistics(Owner owner);

    /** 同时统计待发送、发送中与未验证结果；unknown/submitted_unverified 不视为已确认送达。 */
    @Select(
            """
            SELECT count(*) FILTER(WHERE n.status='pending') AS pending,
              count(*) FILTER(WHERE n.status='sending') AS sending,
              count(*) FILTER(WHERE n.status='unknown') AS unknown,
              count(*) FILTER(WHERE n.status='submitted_unverified') AS submitted_unverified
            FROM channel.notices n JOIN channel.bindings b ON b.id=n.binding_id
            WHERE b.tenant=#{tenant} AND b.subject=#{subject}
            """)
    Map<String, Object> getNotificationStatistics(Owner owner);
}
