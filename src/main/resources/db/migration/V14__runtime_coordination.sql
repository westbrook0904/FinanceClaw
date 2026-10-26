-- 描述执行资源；Command/Attempt 仍是唯一执行权威，registry 失联不可续活 command。
CREATE TABLE business.runtime_workers (
    worker_id text PRIMARY KEY,
    protocol_version integer NOT NULL CHECK(protocol_version > 0),
    runtime_hash text NOT NULL CHECK(runtime_hash ~ '^[a-f0-9]{64}$'),
    capabilities text[] NOT NULL,
    parallelism integer NOT NULL CHECK(parallelism BETWEEN 1 AND 32),
    active_attempts integer NOT NULL CHECK(active_attempts BETWEEN 0 AND parallelism),
    state text NOT NULL CHECK(state IN ('active','stopped')),
    heartbeat_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX runtime_workers_live ON business.runtime_workers(expires_at) WHERE state='active';

-- 不可变工件目录只登记受控根目录内的相对路径；路径不提供代码执行授权。
CREATE TABLE business.runtime_artifacts (
    artifact_hash text PRIMARY KEY CHECK(artifact_hash ~ '^[a-f0-9]{64}$'),
    runtime_hash text NOT NULL CHECK(runtime_hash ~ '^[a-f0-9]{64}$'),
    relative_path text NOT NULL,
    bytes bigint NOT NULL CHECK(bytes > 0),
    protocol_version integer NOT NULL CHECK(protocol_version > 0),
    capabilities text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TRIGGER runtime_artifacts_immutable BEFORE UPDATE OR DELETE ON business.runtime_artifacts
    FOR EACH ROW EXECUTE FUNCTION control_plane.reject_immutable_mutation();

-- 通知仅含 Turn UUID 或全局标记；PostgreSQL 在事务提交后交付，事实仍由原表回放。
CREATE FUNCTION business.notify_task_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('financeclaw_task_events',
        CASE WHEN NEW.turn_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN NEW.turn_id ELSE '*' END);
    RETURN NULL;
END;
$$;
CREATE TRIGGER task_event_committed AFTER INSERT ON business.events
    FOR EACH ROW EXECUTE FUNCTION business.notify_task_event();

CREATE FUNCTION business.notify_task_access_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('financeclaw_task_events','*');
    RETURN NULL;
END;
$$;
CREATE TRIGGER task_grant_changed AFTER UPDATE OR DELETE ON business.grants
    FOR EACH ROW EXECUTE FUNCTION business.notify_task_access_changed();
CREATE TRIGGER task_resource_grant_changed AFTER INSERT OR UPDATE OR DELETE ON control_plane.resource_grants
    FOR EACH ROW EXECUTE FUNCTION business.notify_task_access_changed();
CREATE TRIGGER task_agent_archived AFTER UPDATE OF archived OR DELETE ON control_plane.agents
    FOR EACH ROW EXECUTE FUNCTION business.notify_task_access_changed();
CREATE TRIGGER task_privacy_owner_changed AFTER UPDATE OF epoch,purge_pending OR DELETE ON memory.owners
    FOR EACH ROW EXECUTE FUNCTION business.notify_task_access_changed();

CREATE FUNCTION business.notify_task_source_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_id text;
BEGIN
    source_id := CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
    PERFORM pg_notify('financeclaw_task_events',
        CASE WHEN source_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN source_id ELSE '*' END);
    RETURN NULL;
END;
$$;
CREATE TRIGGER task_privacy_source_changed AFTER UPDATE OF epoch,status OR DELETE ON memory.sources
    FOR EACH ROW EXECUTE FUNCTION business.notify_task_source_changed();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='financeclaw_app') THEN
        GRANT SELECT,INSERT,UPDATE ON business.runtime_workers TO financeclaw_app;
        GRANT SELECT,INSERT ON business.runtime_artifacts TO financeclaw_app;
    END IF;
END $$;
