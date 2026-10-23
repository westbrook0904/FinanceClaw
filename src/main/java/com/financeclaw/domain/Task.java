package com.financeclaw.domain;

import java.util.List;
import java.util.Set;

/** 任务生命周期的领域值与不变量：区分受理回执、命令租约、执行尝试、封存结果及可回放事件。 */
public final class Task {
    private Task() {}

    public record Owner(String tenant, String subject) {
        public Owner {
            if (tenant == null || tenant.isBlank() || subject == null || subject.isBlank()) {
                throw new IllegalArgumentException("Missing owner");
            }
        }
    }

    public record Turn(
            String id,
            String conversationId,
            String tenant,
            String subject,
            String idempotencyKey,
            String requestHash,
            String message,
            String releaseHash,
            String status,
            long revision,
            String result,
            String reason,
            String agentId) {
        /** 旧任务及现有内置调用保持原 Agent 身份，避免改变历史检查点的命名空间。 */
        public Turn(
                String id,
                String conversationId,
                String tenant,
                String subject,
                String idempotencyKey,
                String requestHash,
                String message,
                String releaseHash,
                String status,
                long revision,
                String result,
                String reason) {
            this(
                    id,
                    conversationId,
                    tenant,
                    subject,
                    idempotencyKey,
                    requestHash,
                    message,
                    releaseHash,
                    status,
                    revision,
                    result,
                    reason,
                    "finance_agent");
        }

        public Turn {
            if (agentId == null || agentId.isBlank()) {
                throw new IllegalArgumentException("Missing agent identity");
            }
        }

        public boolean terminal() {
            return Set.of("completed", "failed", "cancelled").contains(status);
        }

        public Accepted accepted(boolean replay) {
            return new Accepted(conversationId, id, status, revision, replay);
        }

        public Accepted replayAdmission(String hash) {
            if (!requestHash.equals(hash)) {
                throw new Problem(409, "idempotency_conflict");
            }
            return new Accepted(conversationId, id, "queued", 1, true);
        }

        public void requireClaimable() {
            if (!Set.of("queued", "resuming").contains(status)) {
                throw new IllegalStateException("Command/Turn mismatch");
            }
        }
    }

    public record Command(
            String id,
            String turnId,
            String status,
            String attemptId,
            long fence,
            boolean live,
            String resumeId) {}

    public record Claim(Command command, Turn turn) {
        public void requireCurrent(Command now) {
            if (now == null
                    || !now.live()
                    || !now.status().equals("running")
                    || now.fence() != command.fence()
                    || !java.util.Objects.equals(now.attemptId(), command.attemptId())) {
                throw new Fenced();
            }
        }

        public ExecutionScope scope() {
            return new ExecutionScope(
                    turn.tenant(),
                    turn.subject(),
                    ExecutionScope.hash(
                            List.of(
                                    turn.conversationId(),
                                    turn.id(),
                                    command.id(),
                                    command.attemptId())),
                    turn.agentId(),
                    turn.releaseHash());
        }
    }

    public record Receipt(
            String id,
            String commandId,
            long fence,
            Long stateVersion,
            String outcome,
            String result) {}

    public record Outcome(
            String status, String result, long stateVersion, List<Governance.Pending> pending) {
        /** 普通完成或失败结果不包含待回答交互；暂停结果必须使用完整构造器提供交互批次。 */
        public Outcome(String status, String result, long version) {
            this(status, result, version, List.of());
        }
    }

    public record Accepted(
            String conversationId,
            String turnId,
            String status,
            long revision,
            boolean idempotentReplay) {}

    public record Event(
            String turnId,
            long revision,
            String status,
            String reason,
            String kind,
            String text,
            String tool) {
        /** 业务状态事件具有持久修订号；流式增量使用完整构造器声明独立事件种类。 */
        public Event(String turnId, long revision, String status, String reason) {
            this(turnId, revision, status, reason, "turn", null, null);
        }
    }

    public record Journal(
            long sequence, String conversationId, String turnId, String role, String content) {}

    public static final class Problem extends RuntimeException {
        private final int status;

        public Problem(int status, String code) {
            super(code);
            this.status = status;
        }

        public int status() {
            return status;
        }
    }

    public static final class Fenced extends RuntimeException {
        public Fenced() {
            super("Execution lease is no longer current");
        }
    }
}
