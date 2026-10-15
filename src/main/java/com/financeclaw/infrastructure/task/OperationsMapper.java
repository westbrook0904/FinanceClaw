package com.financeclaw.infrastructure.task;

import com.financeclaw.domain.Task.Owner;
import java.util.Map;
import org.apache.ibatis.annotations.*;

/** Read-only, owner-scoped operating facts; no messages, tool arguments or credentials. */
@Mapper
public interface OperationsMapper {
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
      (SELECT count(*) FROM business.tool_calls WHERE turn_id IN (SELECT id FROM owned) AND status='pending') AS uncertain_effects,
      (SELECT count(*) FROM business.outbox WHERE turn_id IN (SELECT id FROM owned) AND NOT delivered) AS pending_outbox,
      (SELECT count(*) FROM channel.notices n JOIN channel.bindings b ON b.id=n.binding_id
        WHERE b.tenant=#{tenant} AND b.subject=#{subject} AND n.status IN ('pending','sending')) AS pending_notifications,
      (SELECT count(*) FROM channel.notices n JOIN channel.bindings b ON b.id=n.binding_id
        WHERE b.tenant=#{tenant} AND b.subject=#{subject} AND n.status IN ('unknown','submitted_unverified')) AS unverified_notifications,
      (SELECT count(*) FROM channel.bindings b JOIN owned t ON t.id=b.active_turn
        WHERE b.tenant=#{tenant} AND b.subject=#{subject} AND NOT b.muted
          AND t.status IN ('waiting','blocked','completed','failed','cancelled')
          AND NOT EXISTS(SELECT 1 FROM channel.notices n
            WHERE n.turn_id=t.id AND n.revision=t.revision AND n.status<>'suppressed')) AS unprojected_notifications,
      (SELECT count(*) FROM memory.jobs WHERE tenant=#{tenant} AND subject=#{subject}
        AND status NOT IN ('done','obsolete','forgotten')) AS pending_memory_jobs,
      (SELECT count(*) FROM memory.owners WHERE tenant=#{tenant} AND subject=#{subject} AND purge_pending) AS pending_forget
    FROM owned
    """)
  Map<String, Object> drain(Owner owner);

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
  Map<String, Object> tasks(Owner owner);

  @Select(
      """
    SELECT COALESCE(sum(p.model_calls),0) AS model_calls, COALESCE(sum(p.tool_calls),0) AS tool_calls
    FROM business.turn_policies p JOIN business.turns t ON t.id=p.turn_id
    WHERE t.tenant=#{tenant} AND t.subject=#{subject}
    """)
  Map<String, Object> calls(Owner owner);

  @Select(
      """
    SELECT count(*) AS pending FROM business.outbox o JOIN business.turns t ON t.id=o.turn_id
    WHERE NOT o.delivered AND t.tenant=#{tenant} AND t.subject=#{subject}
    """)
  Map<String, Object> outbox(Owner owner);

  @Select(
      """
    SELECT count(*) FILTER(WHERE n.status='pending') AS pending,
      count(*) FILTER(WHERE n.status='sending') AS sending,
      count(*) FILTER(WHERE n.status='unknown') AS unknown,
      count(*) FILTER(WHERE n.status='submitted_unverified') AS submitted_unverified
    FROM channel.notices n JOIN channel.bindings b ON b.id=n.binding_id
    WHERE b.tenant=#{tenant} AND b.subject=#{subject}
    """)
  Map<String, Object> notifications(Owner owner);
}
