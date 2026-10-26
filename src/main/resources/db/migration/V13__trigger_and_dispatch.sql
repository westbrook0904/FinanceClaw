-- Trigger receipts reference ordinary Turns; no competing execution lifecycle is introduced.
CREATE TABLE control_plane.trigger_rules (
 id text PRIMARY KEY,
 tenant text NOT NULL, subject text NOT NULL,
 revision bigint NOT NULL CHECK(revision>0), enabled boolean NOT NULL,
 kind text NOT NULL CHECK(kind IN ('cron','webhook')),
 expression text, zone text,
 misfire text NOT NULL CHECK(misfire IN ('skip','fire_once','catch_up')),
 overlap text NOT NULL CHECK(overlap IN ('allow','skip')),
 agent_id text NOT NULL,
 version_policy text NOT NULL CHECK(version_policy IN ('fixed','current')),
 release_hash text REFERENCES control_plane.release_manifests(release_hash),
 message text NOT NULL CHECK(length(message) BETWEEN 1 AND 32000),
 priority int NOT NULL CHECK(priority BETWEEN -100 AND 100),
 next_at timestamptz, retry_after timestamptz, last_error text,
 CHECK((version_policy='fixed')=(release_hash IS NOT NULL)),
 CHECK((kind='cron' AND expression IS NOT NULL AND zone IS NOT NULL AND next_at IS NOT NULL)
       OR (kind='webhook' AND expression IS NULL AND zone IS NULL AND next_at IS NULL))
);
CREATE INDEX trigger_due ON control_plane.trigger_rules(next_at,id) WHERE enabled AND kind='cron';
CREATE INDEX trigger_owner ON control_plane.trigger_rules(tenant,subject,id);
CREATE TABLE control_plane.trigger_rule_revisions (
 rule_id text NOT NULL REFERENCES control_plane.trigger_rules(id),
 revision bigint NOT NULL, definition jsonb NOT NULL, created_at timestamptz NOT NULL,
 PRIMARY KEY(rule_id,revision)
);
CREATE TRIGGER trigger_rule_revisions_immutable BEFORE UPDATE OR DELETE ON control_plane.trigger_rule_revisions
 FOR EACH ROW EXECUTE FUNCTION control_plane.reject_immutable_mutation();
CREATE TABLE control_plane.trigger_firings (
 rule_id text NOT NULL REFERENCES control_plane.trigger_rules(id), rule_revision bigint NOT NULL,
 event_key text NOT NULL CHECK(length(event_key)<=600), scheduled_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('accepted','skipped_misfire','skipped_overlap')),
 conversation_id text REFERENCES business.conversations(id), turn_id text REFERENCES business.turns(id),
 release_hash text REFERENCES control_plane.release_manifests(release_hash), coalesced_until timestamptz,
 PRIMARY KEY(rule_id,event_key),
 FOREIGN KEY(rule_id,rule_revision) REFERENCES control_plane.trigger_rule_revisions(rule_id,revision),
 CHECK((status='accepted' AND conversation_id IS NOT NULL AND turn_id IS NOT NULL AND release_hash IS NOT NULL)
       OR (status<>'accepted' AND conversation_id IS NULL AND turn_id IS NULL AND release_hash IS NULL))
);
CREATE INDEX trigger_firing_turn ON control_plane.trigger_firings(turn_id) WHERE turn_id IS NOT NULL;
CREATE INDEX trigger_firing_history ON control_plane.trigger_firings(rule_id,scheduled_at,event_key);
CREATE TRIGGER trigger_firings_immutable BEFORE UPDATE OR DELETE ON control_plane.trigger_firings
 FOR EACH ROW EXECUTE FUNCTION control_plane.reject_immutable_mutation();
CREATE SEQUENCE business.dispatch_sequence;
CREATE TABLE business.dispatch_policies (
 tenant text NOT NULL, subject text NOT NULL, revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 maximum_running int NOT NULL DEFAULT 4 CHECK(maximum_running BETWEEN 1 AND 1024),
 maximum_per_agent int NOT NULL DEFAULT 2 CHECK(maximum_per_agent BETWEEN 1 AND maximum_running),
 last_sequence bigint NOT NULL DEFAULT 0, PRIMARY KEY(tenant,subject)
);
CREATE TABLE business.dispatch_metadata (
 turn_id text PRIMARY KEY REFERENCES business.turns(id), priority int NOT NULL CHECK(priority BETWEEN -100 AND 100)
);
CREATE TABLE business.dispatch_tenant_limits (
 tenant text PRIMARY KEY,revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 maximum_running int NOT NULL DEFAULT 64 CHECK(maximum_running BETWEEN 1 AND 4096)
);
CREATE TABLE business.dispatch_agent_limits (
 tenant text NOT NULL,agent_id text NOT NULL,
 revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 maximum_running int NOT NULL DEFAULT 1024 CHECK(maximum_running BETWEEN 1 AND 4096),
 PRIMARY KEY(tenant,agent_id)
);
DO $$
BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='financeclaw_app') THEN
  GRANT SELECT,INSERT,UPDATE ON control_plane.trigger_rules TO financeclaw_app;
  GRANT SELECT,INSERT ON control_plane.trigger_rule_revisions,control_plane.trigger_firings TO financeclaw_app;
  GRANT SELECT,INSERT,UPDATE ON business.dispatch_policies,business.dispatch_metadata,business.dispatch_tenant_limits,business.dispatch_agent_limits TO financeclaw_app;
  GRANT USAGE,SELECT ON SEQUENCE business.dispatch_sequence TO financeclaw_app;
 END IF;
END;
$$;
