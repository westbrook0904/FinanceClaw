package com.financeclaw.infrastructure.scheduling;

import com.financeclaw.app.scheduling.port.TriggerRepository;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.scheduling.TriggerFiring;
import com.financeclaw.domain.scheduling.TriggerRule;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;

public final class JdbcTriggerRepository implements TriggerRepository {
    private final JdbcTemplate jdbc;

    public JdbcTriggerRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    private static Instant instant(ResultSet rs, String name) throws SQLException {
        Timestamp t = rs.getTimestamp(name);
        return t == null ? null : t.toInstant();
    }

    private static Timestamp timestamp(Instant i) {
        return i == null ? null : Timestamp.from(i);
    }

    private static TriggerRule rule(ResultSet r, int row) throws SQLException {
        return new TriggerRule(
                r.getString("id"),
                r.getString("tenant"),
                r.getString("subject"),
                r.getLong("revision"),
                r.getBoolean("enabled"),
                r.getString("kind"),
                r.getString("expression"),
                r.getString("zone"),
                r.getString("misfire"),
                r.getString("overlap"),
                r.getString("agent_id"),
                r.getString("version_policy"),
                r.getString("release_hash"),
                r.getString("message"),
                r.getInt("priority"),
                instant(r, "next_at"),
                instant(r, "retry_after"),
                r.getString("last_error"));
    }

    private static TriggerFiring firing(ResultSet r, int row) throws SQLException {
        return new TriggerFiring(
                r.getString("rule_id"),
                r.getLong("rule_revision"),
                r.getString("event_key"),
                instant(r, "scheduled_at"),
                r.getString("status"),
                r.getString("conversation_id"),
                r.getString("turn_id"),
                r.getString("release_hash"),
                instant(r, "coalesced_until"));
    }

    @Override
    public Instant now() {
        return jdbc.queryForObject("SELECT clock_timestamp()", Timestamp.class).toInstant();
    }

    @Override
    public TriggerRule owned(Owner o, String id, boolean lock) {
        var values =
                jdbc.query(
                        "SELECT * FROM control_plane.trigger_rules WHERE id=? AND tenant=? AND subject=?"
                                + (lock ? " FOR UPDATE" : ""),
                        JdbcTriggerRepository::rule,
                        id,
                        o.tenant(),
                        o.subject());
        return values.isEmpty() ? null : values.getFirst();
    }

    @Override
    public TriggerRule nextDue() {
        var rules =
                jdbc.query(
                        "SELECT * FROM control_plane.trigger_rules WHERE enabled AND kind='cron' AND next_at<=clock_timestamp() AND (retry_after IS NULL OR retry_after<=clock_timestamp()) ORDER BY next_at,id LIMIT 1 FOR UPDATE SKIP LOCKED",
                        JdbcTriggerRepository::rule);
        return rules.isEmpty() ? null : rules.getFirst();
    }

    @Override
    public List<TriggerRule> list(Owner o, String after, int limit) {
        return jdbc.query(
                "SELECT * FROM control_plane.trigger_rules WHERE tenant=? AND subject=? AND id>? ORDER BY id LIMIT ?",
                JdbcTriggerRepository::rule,
                o.tenant(),
                o.subject(),
                after,
                limit);
    }

    private Object[] args(TriggerRule r) {
        return new Object[] {
            r.tenant(),
            r.subject(),
            r.revision(),
            r.enabled(),
            r.kind(),
            r.expression(),
            r.zone(),
            r.misfire(),
            r.overlap(),
            r.agentId(),
            r.versionPolicy(),
            r.releaseHash(),
            r.message(),
            r.priority(),
            timestamp(r.nextAt()),
            r.id()
        };
    }

    @Override
    public void insert(TriggerRule r) {
        int count =
                jdbc.update(
                        "INSERT INTO control_plane.trigger_rules(tenant,subject,revision,enabled,kind,expression,zone,misfire,overlap,agent_id,version_policy,release_hash,message,priority,next_at,id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                        args(r));
        if (count != 1) throw new Problem(409, "schedule_revision_changed");
        historyRevision(r);
    }

    @Override
    public boolean replace(TriggerRule r, long expected) {
        var args = new java.util.ArrayList<>(java.util.Arrays.asList(args(r)));
        args.add(expected);
        boolean changed =
                jdbc.update(
                                "UPDATE control_plane.trigger_rules SET tenant=?,subject=?,revision=?,enabled=?,kind=?,expression=?,zone=?,misfire=?,overlap=?,agent_id=?,version_policy=?,release_hash=?,message=?,priority=?,next_at=?,retry_after=NULL,last_error=NULL WHERE id=? AND revision=?",
                                args.toArray())
                        == 1;
        if (changed) historyRevision(r);
        return changed;
    }

    private void historyRevision(TriggerRule r) {
        jdbc.update(
                "INSERT INTO control_plane.trigger_rule_revisions SELECT id,revision,to_jsonb(trigger_rules),clock_timestamp() FROM control_plane.trigger_rules WHERE id=? AND revision=?",
                r.id(),
                r.revision());
    }

    @Override
    public void advance(String id, long rev, Instant next) {
        if (jdbc.update(
                        "UPDATE control_plane.trigger_rules SET next_at=?,retry_after=NULL,last_error=NULL WHERE id=? AND revision=?",
                        timestamp(next),
                        id,
                        rev)
                != 1) throw new Problem(409, "schedule_revision_changed");
    }

    @Override
    public void defer(String id, long rev, Instant occurrence, String reason) {
        jdbc.update(
                "UPDATE control_plane.trigger_rules SET retry_after=clock_timestamp()+interval '30 seconds',last_error=? WHERE id=? AND revision=? AND next_at=? AND enabled AND kind='cron'",
                reason,
                id,
                rev,
                timestamp(occurrence));
    }

    @Override
    public TriggerFiring replay(String id, String key) {
        var rows =
                jdbc.query(
                        "SELECT * FROM control_plane.trigger_firings WHERE rule_id=? AND event_key=?",
                        JdbcTriggerRepository::firing,
                        id,
                        key);
        return rows.isEmpty() ? null : rows.getFirst();
    }

    @Override
    public boolean active(String id) {
        return Boolean.TRUE.equals(
                jdbc.queryForObject(
                        "SELECT EXISTS(SELECT 1 FROM control_plane.trigger_firings f JOIN business.turns t ON t.id=f.turn_id WHERE f.rule_id=? AND t.status IN ('queued','resuming','running','waiting','cancelling','blocked'))",
                        Boolean.class,
                        id));
    }

    @Override
    public void record(TriggerFiring r) {
        jdbc.update(
                "INSERT INTO control_plane.trigger_firings(rule_id,rule_revision,event_key,scheduled_at,status,conversation_id,turn_id,release_hash,coalesced_until) VALUES(?,?,?,?,?,?,?,?,?)",
                r.ruleId(),
                r.ruleRevision(),
                r.eventKey(),
                timestamp(r.scheduledAt()),
                r.status(),
                r.conversationId(),
                r.turnId(),
                r.releaseHash(),
                timestamp(r.coalescedUntil()));
    }

    @Override
    public void priority(String turn, int priority) {
        jdbc.update(
                "INSERT INTO business.dispatch_metadata(turn_id,priority) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET priority=excluded.priority",
                turn,
                priority);
    }

    @Override
    public String release(String turn) {
        return jdbc.queryForObject(
                "SELECT release_hash FROM business.turns WHERE id=?", String.class, turn);
    }

    @Override
    public List<TriggerFiring> history(String id, Instant after, String afterKey, int limit) {
        return jdbc.query(
                "SELECT * FROM control_plane.trigger_firings WHERE rule_id=? AND (scheduled_at,event_key)>(?,?) ORDER BY scheduled_at,event_key LIMIT ?",
                JdbcTriggerRepository::firing,
                id,
                timestamp(after),
                afterKey,
                limit);
    }
}
