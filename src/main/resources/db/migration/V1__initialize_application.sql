-- FinanceClaw 独立应用的新库初始化。
-- 本文件直接定义当前数据模型，由 Flyway 在空库执行一次；不存在旧应用数据回填或历史表升级。
-- 主库共 35 张应用表：business 16、agent_runtime 2、memory 13、channel 4。
-- 独立遗忘账本使用 privacy-ledger 下的初始化文件，不在本业务库创建。

-- 一、职责隔离：业务权威、框架检查点、跨会话记忆、渠道接入分别使用独立 schema。
-- 受限记忆 Worker 只获得 memory 的必要权限，具体授权由部署初始化脚本负责。
CREATE SCHEMA business;
CREATE SCHEMA agent_runtime;
CREATE SCHEMA memory;
CREATE SCHEMA channel;

-- 二、会话与接入治理。
-- 会话是 owner 隔离和同会话串行受理的锁对象；授权版本与接入开关各有独立并发边界。
CREATE TABLE business.conversations (
  id text PRIMARY KEY,
  tenant text NOT NULL,
  subject text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- 接入暂停只阻止新的 Turn；执行中任务及其人工回答、取消、恢复继续使用正常链路。
CREATE TABLE business.admission (
  tenant text NOT NULL,
  subject text NOT NULL,
  paused boolean NOT NULL DEFAULT false,
  revision bigint NOT NULL DEFAULT 0 CHECK(revision >= 0),
  PRIMARY KEY(tenant,subject)
);
CREATE TABLE business.admission_changes (
  tenant text NOT NULL,
  subject text NOT NULL,
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  paused boolean NOT NULL,
  revision bigint NOT NULL CHECK(revision > 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(tenant,subject,idempotency_key),
  UNIQUE(tenant,subject,revision),
  FOREIGN KEY(tenant,subject) REFERENCES business.admission(tenant,subject)
);

-- 当前授权与不可变变更回执分开；重放旧请求必须返回当时结果，不能返回最新授权。
CREATE TABLE business.grants (
  conversation_id text PRIMARY KEY REFERENCES business.conversations(id),
  revision bigint NOT NULL DEFAULT 1,
  scopes jsonb NOT NULL
);
CREATE TABLE business.grant_changes (
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  revision bigint NOT NULL,
  scopes jsonb NOT NULL,
  PRIMARY KEY(conversation_id,idempotency_key)
);

-- 三、任务、调度和执行尝试。
-- Turn 保存业务状态与请求幂等身份；Command 保存领取、租约和 fence；Attempt 保存一次尝试的封存回执。
-- Turn 等待人工回答时 Command 已经完成本轮调度，二者状态不能共用一个字段。
CREATE TABLE business.turns (
  id text PRIMARY KEY,
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  tenant text NOT NULL,
  subject text NOT NULL,
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  message text NOT NULL,
  release_hash text NOT NULL,
  status text NOT NULL CHECK(status IN ('queued','resuming','running','waiting','cancelling','blocked','completed','failed','cancelled')),
  revision bigint NOT NULL DEFAULT 1,
  result text,
  reason text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(conversation_id,idempotency_key)
);
CREATE UNIQUE INDEX one_active_turn ON business.turns(conversation_id)
  WHERE status IN ('queued','resuming','running','waiting','cancelling','blocked');
CREATE INDEX turn_owner_status ON business.turns(tenant,subject,status);
CREATE INDEX turn_completed_history ON business.turns(conversation_id,created_at DESC,id DESC)
  WHERE status='completed';

CREATE TABLE business.commands (
  id text PRIMARY KEY,
  turn_id text NOT NULL UNIQUE REFERENCES business.turns(id),
  status text NOT NULL CHECK(status IN ('queued','running','done')),
  attempt_id text,
  fence bigint NOT NULL DEFAULT 0,
  lease_until timestamptz,
  worker_id text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  resume_id text
);
CREATE INDEX command_queue ON business.commands(created_at) WHERE status='queued';
CREATE INDEX command_expiry ON business.commands(lease_until) WHERE status='running';
CREATE TABLE business.attempts (
  id text PRIMARY KEY,
  command_id text NOT NULL REFERENCES business.commands(id),
  fence bigint NOT NULL,
  state_version bigint,
  outcome text,
  result text,
  started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  resume_id text,
  UNIQUE(command_id,fence)
);

-- 每个 Turn 在受理时冻结授权快照；后续模型/工具预算原子扣减，不能直接读取当前 grant 替代。
CREATE TABLE business.turn_policies (
  turn_id text PRIMARY KEY REFERENCES business.turns(id),
  grant_revision bigint NOT NULL,
  scopes jsonb NOT NULL,
  model_calls int NOT NULL DEFAULT 0,
  tool_calls int NOT NULL DEFAULT 0
);

-- 人工审批/输入绑定具体尝试、检查点、发布、授权及动作哈希；一个尝试可同时有多条交互。
CREATE TABLE business.interactions (
  id text PRIMARY KEY,
  turn_id text NOT NULL REFERENCES business.turns(id),
  command_id text NOT NULL REFERENCES business.commands(id),
  attempt_id text NOT NULL REFERENCES business.attempts(id),
  state_version bigint NOT NULL,
  grant_revision bigint NOT NULL,
  release_hash text NOT NULL,
  kind text NOT NULL CHECK(kind IN ('approval','input')),
  call_id text NOT NULL,
  tool text NOT NULL,
  action_hash text NOT NULL,
  schema_json text NOT NULL,
  question text NOT NULL,
  revision bigint NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','answered','invalid')),
  expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '15 minutes',
  response_hash text,
  response_json text,
  UNIQUE(attempt_id,call_id)
);
-- 新库初始化的循环外键绑定：交互引用 Command/Attempt，恢复引用对应交互。
-- 必须等三个表都存在后添加这两个约束；这两条 ALTER 不是历史数据升级步骤。
ALTER TABLE business.commands ADD CONSTRAINT commands_resume_id_fkey
  FOREIGN KEY(resume_id) REFERENCES business.interactions(id);
ALTER TABLE business.attempts ADD CONSTRAINT attempts_resume_id_fkey
  FOREIGN KEY(resume_id) REFERENCES business.interactions(id);

-- 四、用户可见历史与取消回执。
-- Journal 的递增游标用于消息分页；Event 的 Turn revision 用于持久业务事件重连。
-- 实时模型增量通过 AgentScope/进程内事件传送，不逐 token 写库。
CREATE TABLE business.journal (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  turn_id text NOT NULL REFERENCES business.turns(id),
  role text NOT NULL,
  content text NOT NULL,
  UNIQUE(turn_id,role)
);
CREATE TABLE business.events (
  turn_id text NOT NULL REFERENCES business.turns(id),
  revision bigint NOT NULL,
  status text NOT NULL,
  reason text,
  kind text NOT NULL DEFAULT 'turn',
  text text,
  tool text,
  PRIMARY KEY(turn_id,revision)
);
CREATE TABLE business.cancel_requests (
  turn_id text NOT NULL REFERENCES business.turns(id),
  idempotency_key text NOT NULL,
  status text NOT NULL,
  revision bigint NOT NULL,
  PRIMARY KEY(turn_id,idempotency_key)
);

-- 五、业务产物、原子报告发布、确定性步骤结果。
-- Artifact 支持大对象独立授权和分页读取；报告实体及其精确幂等回执通过同一次 INSERT 原子创建。
CREATE TABLE business.artifacts (
  id text PRIMARY KEY,
  turn_id text NOT NULL REFERENCES business.turns(id),
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  tenant text NOT NULL,
  subject text NOT NULL,
  release_hash text NOT NULL,
  hash text NOT NULL,
  payload text NOT NULL,
  bytes int NOT NULL CHECK(bytes BETWEEN 1 AND 2097152),
  required_scope text NOT NULL DEFAULT 'artifact:read',
  privacy_bound boolean NOT NULL DEFAULT false,
  CHECK(octet_length(payload)=bytes)
);
CREATE TABLE business.reports (
  id text NOT NULL UNIQUE,
  turn_id text NOT NULL REFERENCES business.turns(id),
  call_id text NOT NULL,
  title text NOT NULL,
  action_hash text NOT NULL,
  result text NOT NULL,
  PRIMARY KEY(turn_id,call_id)
);
COMMENT ON TABLE business.reports IS '完整的本地报告发布及原始幂等结果；不存在半成品或单独回执行';
COMMENT ON COLUMN business.reports.result IS '首次成功发布返回的精确回执文本，重复调用直接回放，不重新生成';

-- 步骤表缓存已成功完成的本地确定性计算，输入哈希变化时拒绝复用。
CREATE TABLE business.capability_steps (
  turn_id text NOT NULL REFERENCES business.turns(id) ON DELETE CASCADE,
  namespace text NOT NULL,
  step text NOT NULL,
  input_hash text NOT NULL,
  result text NOT NULL,
  PRIMARY KEY(turn_id,namespace,step)
);

-- 六、框架检查点。
-- CAS 版本和墓碑防止并发覆盖及删除后从空状态意外重跑；保存检查点不等于业务完成。
CREATE TABLE agent_runtime.states (
  namespace varchar(64) NOT NULL,
  user_id varchar(64) NOT NULL,
  session_id varchar(64) NOT NULL,
  state_key varchar(128) NOT NULL,
  version bigint NOT NULL CHECK(version > 0),
  payload jsonb,
  deleted boolean NOT NULL DEFAULT false,
  PRIMARY KEY(namespace,user_id,session_id,state_key),
  CHECK(deleted=(payload IS NULL))
);

-- 每个 Turn 保存一份有界工作上下文和本轮完整执行轨迹。下一轮只读取最新已完成
-- Turn 的工作投影；transcript 保留本轮问答、调用参数与结果引用，不重复复制整段会话。
-- 与 SDK 检查点在同一执行栅栏事务中写入，未完成/失败尝试不能冒充已提交的会话历史。
CREATE TABLE agent_runtime.work_contexts (
  turn_id text PRIMARY KEY REFERENCES business.turns(id) ON DELETE CASCADE,
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL CHECK(epoch >= 0),
  release_hash text NOT NULL,
  grant_revision bigint NOT NULL,
  attempt_id text NOT NULL REFERENCES business.attempts(id),
  state_version bigint NOT NULL CHECK(state_version > 0),
  payload jsonb NOT NULL,
  transcript jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX work_context_conversation ON agent_runtime.work_contexts(conversation_id);
CREATE INDEX work_context_owner ON agent_runtime.work_contexts(tenant,subject,epoch);

-- 七、两层跨会话记忆。
-- owners 为高频加锁的隐私/画像版本窄行；Markdown 画像仅由用户手工维护，证据提取不覆盖画像。
CREATE TABLE memory.owners (
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL DEFAULT 1,
  purge_pending boolean NOT NULL DEFAULT false,
  profile_revision bigint NOT NULL DEFAULT 0 CHECK(profile_revision >= 0),
  PRIMARY KEY(tenant,subject)
);
CREATE TABLE memory.user_profiles (
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL CHECK(epoch >= 1),
  revision bigint NOT NULL CHECK(revision >= 1),
  markdown text NOT NULL CHECK(octet_length(markdown) <= 8192),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(tenant,subject),
  FOREIGN KEY(tenant,subject) REFERENCES memory.owners(tenant,subject) ON DELETE CASCADE
);

-- 每个 Turn 都绑定受理时的 epoch/profile_revision。
-- 关闭自动证据提取时 content 为空、status 为 profile_only，仍支持当前画像与隐私执行校验。
CREATE TABLE memory.sources (
  id text PRIMARY KEY REFERENCES business.turns(id) ON DELETE CASCADE,
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL,
  content text,
  status text NOT NULL DEFAULT 'admitted',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  profile_revision bigint NOT NULL DEFAULT 0 CHECK(profile_revision >= 0)
);
CREATE TABLE memory.facts (
  id text PRIMARY KEY,
  source_id text NOT NULL REFERENCES memory.sources(id) ON DELETE CASCADE,
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL,
  key text NOT NULL,
  content text,
  evidence text,
  revision bigint NOT NULL DEFAULT 1,
  status text NOT NULL CHECK(status IN ('candidate','active','rejected','superseded','forgotten')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(source_id,key)
);
CREATE UNIQUE INDEX memory_active_key ON memory.facts(tenant,subject,key) WHERE status='active';

-- 长期记忆正文按 Turn 仅冻结一次；普通画像编辑不改写，遗忘按 epoch 清理。
-- 包含人工画像，受限记忆 Worker 无权读取或写入此表。
CREATE TABLE memory.turn_snapshots (
  turn_id text PRIMARY KEY REFERENCES business.turns(id) ON DELETE CASCADE,
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL CHECK(epoch>=1),
  profile_revision bigint NOT NULL CHECK(profile_revision>=0),
  context text NOT NULL CHECK(octet_length(context)<=262144),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY(tenant,subject) REFERENCES memory.owners(tenant,subject) ON DELETE CASCADE
);
CREATE INDEX memory_snapshot_owner ON memory.turn_snapshots(tenant,subject,epoch);
CREATE TABLE memory.jobs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL CHECK(kind IN ('extract','index')),
  key_id text NOT NULL,
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL,
  revision bigint NOT NULL DEFAULT 1,
  model_revision text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'pending',
  attempts int NOT NULL DEFAULT 0,
  lease_token text,
  lease_until timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  error_code text,
  UNIQUE(kind,key_id,revision,model_revision)
);
CREATE INDEX memory_job_queue ON memory.jobs(next_attempt_at,id) WHERE status IN ('pending','running');

-- 向量索引是可重建投影；权威事实与模型指纹独立保存。
-- real[] 保持基础初始化不依赖 pgvector；部署开启向量能力后额外创建 HNSW 查询索引。
CREATE TABLE memory.embedding_profiles (
  revision text PRIMARY KEY,
  fingerprint text NOT NULL,
  model text NOT NULL,
  dimensions int NOT NULL CHECK(dimensions=1024)
);
CREATE TABLE memory.projections (
  fact_id text NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL,
  revision bigint NOT NULL,
  model_revision text NOT NULL,
  dimensions int NOT NULL CHECK(dimensions=1024),
  embedding real[] NOT NULL CHECK(array_length(embedding,1)=1024),
  PRIMARY KEY(fact_id,model_revision)
);
CREATE TABLE memory.operations (
  tenant text NOT NULL,
  subject text NOT NULL,
  key text NOT NULL,
  request_hash text NOT NULL,
  result text NOT NULL,
  PRIMARY KEY(tenant,subject,key)
);
CREATE TABLE memory.audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL,
  action text NOT NULL,
  target text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- 八、遗忘恢复屏障。
-- 主库保存与独立账本的绑定、待确认请求和已应用事件；旧备份恢复需要独立账本核对，不能凭 epoch 放行。
CREATE TABLE memory.privacy_ledger_binding (
  singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
  lineage text NOT NULL DEFAULT gen_random_uuid()::text,
  active boolean NOT NULL DEFAULT false
);
INSERT INTO memory.privacy_ledger_binding(singleton) VALUES(true);
CREATE TABLE memory.forget_requests (
  tenant text NOT NULL,
  subject text NOT NULL,
  event_id text NOT NULL UNIQUE,
  lineage text NOT NULL,
  from_epoch bigint NOT NULL CHECK(from_epoch >= 1 AND from_epoch < 9223372036854775807),
  operation_key text NOT NULL,
  request_hash text NOT NULL,
  PRIMARY KEY(tenant,subject),
  FOREIGN KEY(tenant,subject) REFERENCES memory.owners(tenant,subject)
);
CREATE TABLE memory.forget_applied (
  event_id text PRIMARY KEY,
  lineage text NOT NULL,
  tenant text NOT NULL,
  subject text NOT NULL,
  from_epoch bigint NOT NULL CHECK(from_epoch >= 1 AND from_epoch < 9223372036854775807),
  operation_key text NOT NULL,
  request_hash text NOT NULL,
  UNIQUE(tenant,subject,from_epoch),
  FOREIGN KEY(tenant,subject) REFERENCES memory.owners(tenant,subject)
);

-- 九、渠道接入和真实投递。
-- 短期认领凭证、长期身份绑定、入站去重和出站发送结果分别建模。
CREATE TABLE channel.link_codes (
  code_hash text PRIMARY KEY,
  tenant text NOT NULL,
  subject text NOT NULL,
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '5 minutes',
  used boolean NOT NULL DEFAULT false
);
CREATE TABLE channel.bindings (
  id text PRIMARY KEY,
  tenant_key text NOT NULL,
  open_id text NOT NULL,
  chat_id text NOT NULL,
  tenant text NOT NULL,
  subject text NOT NULL,
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  revision bigint NOT NULL DEFAULT 1,
  muted boolean NOT NULL DEFAULT false,
  active_turn text,
  UNIQUE(tenant_key,open_id,chat_id)
);
CREATE TABLE channel.inbox (
  event_id text PRIMARY KEY,
  binding_id text NOT NULL REFERENCES channel.bindings(id),
  request_hash text NOT NULL,
  receipt text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE channel.notices (
  id text PRIMARY KEY,
  turn_id text NOT NULL REFERENCES business.turns(id),
  binding_id text NOT NULL REFERENCES channel.bindings(id),
  binding_revision bigint NOT NULL,
  revision bigint NOT NULL,
  payload text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','submitted_unverified','unknown','suppressed')),
  reason text,
  started_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(turn_id,revision)
);
CREATE INDEX channel_pending_notice ON channel.notices(created_at) WHERE status='pending';
