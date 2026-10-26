package com.financeclaw.infrastructure.scheduling;

import com.financeclaw.app.scheduling.port.DispatchPolicies;
import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.domain.scheduling.ConcurrencyLimit;
import com.financeclaw.domain.scheduling.DispatchPolicy;
import org.springframework.jdbc.core.JdbcTemplate;

/** Serialized short admission accounting; running counts come exclusively from Command leases. */
public final class JdbcCommandDispatch implements DispatchPolicies {
    private final JdbcTemplate jdbc;

    public JdbcCommandDispatch(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    private boolean admissionLock() {
        return Boolean.TRUE.equals(
                jdbc.queryForObject(
                        "SELECT pg_try_advisory_xact_lock(7041840319231)", Boolean.class));
    }

    public Command next(String worker) {
        if (!admissionLock()) return null;
        var rows =
                jdbc.query(
                        """
            WITH running AS MATERIALIZED (
              SELECT rt.tenant,rt.subject,rt.agent_id,count(*) AS items
              FROM business.commands rc JOIN business.turns rt ON rt.id=rc.turn_id
              WHERE rc.status='running' GROUP BY rt.tenant,rt.subject,rt.agent_id
            ), owners AS MATERIALIZED (
              SELECT tenant,subject,sum(items) AS items FROM running GROUP BY tenant,subject
            ), tenants AS MATERIALIZED (
              SELECT tenant,sum(items) AS items FROM running GROUP BY tenant
            ), agents AS MATERIALIZED (
              SELECT tenant,agent_id,sum(items) AS items FROM running GROUP BY tenant,agent_id
            )
            SELECT c.id,c.turn_id,c.status,c.attempt_id,c.fence,false AS live,c.resume_id
            FROM business.commands c JOIN business.turns t ON t.id=c.turn_id
            LEFT JOIN business.dispatch_policies p ON p.tenant=t.tenant AND p.subject=t.subject
            LEFT JOIN business.dispatch_metadata m ON m.turn_id=t.id
            LEFT JOIN business.dispatch_tenant_limits tp ON tp.tenant=t.tenant
            LEFT JOIN business.dispatch_agent_limits ap ON ap.tenant=t.tenant AND ap.agent_id=t.agent_id
            LEFT JOIN running ra ON ra.tenant=t.tenant AND ra.subject=t.subject AND ra.agent_id=t.agent_id
            LEFT JOIN owners ro ON ro.tenant=t.tenant AND ro.subject=t.subject
            LEFT JOIN tenants rt ON rt.tenant=t.tenant
            LEFT JOIN agents ga ON ga.tenant=t.tenant AND ga.agent_id=t.agent_id
            JOIN control_plane.release_manifests r ON r.release_hash=t.release_hash
            JOIN business.runtime_workers w ON w.worker_id=?
            WHERE c.status='queued' AND w.protocol_version=1 AND w.state='active'
              AND w.expires_at>clock_timestamp() AND 'agentscope'=ANY(w.capabilities)
              AND (r.manifest->>'format' IS DISTINCT FROM 'agent-release/3'
                   OR r.manifest->'plan'->>'runtimeArtifactHash'=w.runtime_hash)
              AND COALESCE(rt.items,0)<COALESCE(tp.maximum_running,64)
              AND COALESCE(ro.items,0)<COALESCE(p.maximum_running,4)
              AND COALESCE(ra.items,0)<COALESCE(p.maximum_per_agent,2)
              AND COALESCE(ga.items,0)<COALESCE(ap.maximum_running,1024)
            ORDER BY COALESCE(p.last_sequence,0),
              (COALESCE(m.priority,0)+floor(extract(epoch FROM (clock_timestamp()-c.created_at))/30)) DESC,
              c.created_at,c.id
            LIMIT 1 FOR UPDATE OF c SKIP LOCKED
            """,
                        (r, n) ->
                                new Command(
                                        r.getString("id"),
                                        r.getString("turn_id"),
                                        r.getString("status"),
                                        r.getString("attempt_id"),
                                        r.getLong("fence"),
                                        false,
                                        r.getString("resume_id")),
                        worker);
        return rows.isEmpty() ? null : rows.getFirst();
    }

    public void claimed(Command command, Turn turn, String worker) {
        // Command -> Turn may have waited beyond registration expiry. Lock the actual registry
        // row first and project the database clock afterwards; stop/refresh serialize on this row.
        Boolean eligible =
                jdbc.queryForObject(
                        """
            WITH locked_worker AS MATERIALIZED (
              SELECT * FROM business.runtime_workers WHERE worker_id=? FOR SHARE
            )
            SELECT EXISTS(SELECT 1 FROM locked_worker w
              JOIN control_plane.release_manifests r ON r.release_hash=?
              WHERE w.state='active' AND w.protocol_version=1 AND w.expires_at>clock_timestamp()
                AND 'agentscope'=ANY(w.capabilities)
                AND (r.manifest->>'format' IS DISTINCT FROM 'agent-release/3'
                  OR r.manifest->'plan'->>'runtimeArtifactHash'=w.runtime_hash))
            """,
                        Boolean.class,
                        worker,
                        turn.releaseHash());
        if (!Boolean.TRUE.equals(eligible)) throw new Problem(409, "worker_unavailable");
        jdbc.update(
                "INSERT INTO business.dispatch_policies(tenant,subject,last_sequence) VALUES(?,?,nextval('business.dispatch_sequence')) ON CONFLICT(tenant,subject) DO UPDATE SET last_sequence=nextval('business.dispatch_sequence')",
                turn.tenant(),
                turn.subject());
    }

    @Override
    public DispatchPolicy get(Owner owner) {
        var rows =
                jdbc.query(
                        "SELECT revision,maximum_running,maximum_per_agent FROM business.dispatch_policies WHERE tenant=? AND subject=?",
                        (r, n) -> new DispatchPolicy(r.getLong(1), r.getInt(2), r.getInt(3)),
                        owner.tenant(),
                        owner.subject());
        return rows.isEmpty() ? new DispatchPolicy(0, 4, 2) : rows.getFirst();
    }

    @Override
    public DispatchPolicy update(Owner owner, DispatchPolicy policy) {
        // Same lock as claim prevents quota policy changes racing admission counts.
        jdbc.queryForObject("SELECT pg_advisory_xact_lock(7041840319231)", Object.class);
        jdbc.update(
                "INSERT INTO business.dispatch_policies(tenant,subject) VALUES(?,?) ON CONFLICT DO NOTHING",
                owner.tenant(),
                owner.subject());
        if (jdbc.update(
                        "UPDATE business.dispatch_policies SET revision=revision+1,maximum_running=?,maximum_per_agent=? WHERE tenant=? AND subject=? AND revision=?",
                        policy.maximumRunning(),
                        policy.maximumPerAgent(),
                        owner.tenant(),
                        owner.subject(),
                        policy.revision())
                != 1) throw new Problem(409, "dispatch_revision_changed");
        return get(owner);
    }

    @Override
    public void prioritize(Owner owner, String turn, int priority) {
        // Command -> Turn follows the normal execution lock order; running work is never
        // reprioritized.
        var ids =
                jdbc.queryForList(
                        "SELECT c.id FROM business.commands c JOIN business.turns t ON t.id=c.turn_id WHERE t.id=? AND t.tenant=? AND t.subject=? AND c.status='queued' FOR UPDATE OF c",
                        String.class,
                        turn,
                        owner.tenant(),
                        owner.subject());
        if (ids.isEmpty()) throw new Problem(404, "queued_turn_not_found");
        jdbc.update(
                "INSERT INTO business.dispatch_metadata(turn_id,priority) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET priority=excluded.priority",
                turn,
                priority);
    }

    @Override
    public ConcurrencyLimit tenant(String tenant) {
        var rows =
                jdbc.query(
                        "SELECT revision,maximum_running FROM business.dispatch_tenant_limits WHERE tenant=?",
                        (r, n) -> new ConcurrencyLimit(r.getLong(1), r.getInt(2)),
                        tenant);
        return rows.isEmpty() ? new ConcurrencyLimit(0, 64) : rows.getFirst();
    }

    @Override
    public ConcurrencyLimit updateTenant(String tenant, ConcurrencyLimit limit) {
        jdbc.queryForObject("SELECT pg_advisory_xact_lock(7041840319231)", Object.class);
        jdbc.update(
                "INSERT INTO business.dispatch_tenant_limits(tenant) VALUES(?) ON CONFLICT DO NOTHING",
                tenant);
        if (jdbc.update(
                        "UPDATE business.dispatch_tenant_limits SET revision=revision+1,maximum_running=? WHERE tenant=? AND revision=?",
                        limit.maximumRunning(),
                        tenant,
                        limit.revision())
                != 1) throw new Problem(409, "dispatch_revision_changed");
        return tenant(tenant);
    }

    @Override
    public ConcurrencyLimit agent(Owner owner, String agent) {
        var rows =
                jdbc.query(
                        "SELECT revision,maximum_running FROM business.dispatch_agent_limits WHERE tenant=? AND agent_id=?",
                        (r, n) -> new ConcurrencyLimit(r.getLong(1), r.getInt(2)),
                        owner.tenant(),
                        agent);
        return rows.isEmpty() ? new ConcurrencyLimit(0, 1024) : rows.getFirst();
    }

    @Override
    public ConcurrencyLimit updateAgent(Owner owner, String agent, ConcurrencyLimit limit) {
        jdbc.queryForObject("SELECT pg_advisory_xact_lock(7041840319231)", Object.class);
        jdbc.update(
                "INSERT INTO business.dispatch_agent_limits(tenant,agent_id) VALUES(?,?) ON CONFLICT DO NOTHING",
                owner.tenant(),
                agent);
        if (jdbc.update(
                        "UPDATE business.dispatch_agent_limits SET revision=revision+1,maximum_running=? WHERE tenant=? AND agent_id=? AND revision=?",
                        limit.maximumRunning(),
                        owner.tenant(),
                        agent,
                        limit.revision())
                != 1) throw new Problem(409, "dispatch_revision_changed");
        return agent(owner, agent);
    }
}
