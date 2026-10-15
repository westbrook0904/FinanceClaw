CREATE TABLE business.capability_steps (
  turn_id text NOT NULL REFERENCES business.turns(id) ON DELETE CASCADE,
  namespace text NOT NULL,
  step text NOT NULL,
  input_hash text NOT NULL,
  result text NOT NULL,
  PRIMARY KEY(turn_id,namespace,step)
);
-- References only; Skill instruction bodies never enter this table or AgentState.
CREATE TABLE business.skill_refs (
  turn_id text PRIMARY KEY REFERENCES business.turns(id) ON DELETE CASCADE,
  package_hash text NOT NULL,
  version text NOT NULL,
  resources text NOT NULL DEFAULT '[]'
);
ALTER TABLE business.artifacts ADD COLUMN required_scope text NOT NULL DEFAULT 'artifact:read';
