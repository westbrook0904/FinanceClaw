package com.financeclaw.app.task;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 任务受理入口：在同一短事务内提交 Turn、权限快照、用户记忆版本及执行命令。会话锁阻止同会话重入，共享受理锁允许不同会话并行提交。 */
@Service
public class SubmitTaskService {
    private static final int MAX_MESSAGE_CHARS = 32_000;
    private final TaskAdmissionGateway db;
    private final ExecutionPolicyGateway governance;
    private final ExecutionRelease releases;
    private final ExecutionMemory memory;
    private final TransactionTemplate tx;
    private final AgentService agents;

    public SubmitTaskService(
            TaskAdmissionGateway db,
            ExecutionPolicyGateway governance,
            ExecutionRelease releases,
            ExecutionMemory memory,
            TransactionTemplate tx,
            AgentService agents) {
        this.db = db;
        this.governance = governance;
        this.releases = releases;
        this.memory = memory;
        this.tx = tx;
        this.agents = agents;
    }

    private static String id() {
        return UUID.randomUUID().toString();
    }

    /** 创建用户独占会话，固定发布并初始化授权；三项写入共同提交。 */
    public String createConversation(Owner owner) {
        return createConversation(owner, "finance_agent");
    }

    /** 目标解析、owner 校验与绑定在同一事务内；后续发布只影响新建会话。 */
    public String createConversation(Owner owner, String agentId) {
        String id = id();
        tx.executeWithoutResult(
                s -> {
                    String releaseHash =
                            "finance_agent".equals(agentId)
                                    ? releases.currentHash()
                                    : agents.resolveTarget(owner, agentId).releaseHash();
                    var release = releases.require(releaseHash);
                    db.createConversation(id, owner);
                    if ("finance_agent".equals(agentId)) {
                        bindRelease(id, releaseHash);
                    } else if (db.bindConversationAgent(id, agentId, releaseHash) != 1) {
                        throw new Problem(409, "conversation_release_conflict");
                    }
                    governance.ensureGrant(id, PayloadJson.canonical(release.scopes()));
                });
        return id;
    }

    /** 受理任务并冻结执行身份。幂等回放先于暂停检查；只有全套任务、授权和记忆元数据提交后才可被 Worker 领取。 */
    public Accepted submit(Owner owner, String conversation, String key, String message) {
        new IdempotencyKey(key);
        if (message == null || message.isBlank() || message.length() > MAX_MESSAGE_CHARS) {
            throw new Problem(422, "invalid_message");
        }
        String hash = ExecutionScope.hash(List.of(message));
        return tx.execute(
                s -> {
                    if (db.lockConversation(conversation, owner) == null) {
                        throw new Problem(404, "not_found");
                    }
                    // 阶段一：先验证资源归属和幂等身份，已受理的请求在暂停后仍可读取原回执。
                    Turn old = db.replay(conversation, key);
                    if (old != null) {
                        return old.replayAdmission(hash);
                    }
                    // 受理共享锁持有到提交；暂停必须等待已经受理的写入，而不同会话可以并行提交。
                    db.ensureAdmission(owner);
                    if (db.admissionPaused(owner)) {
                        throw new Problem(503, "admission_paused");
                    }
                    if (db.active(conversation)) {
                        throw new Problem(409, "conversation_busy");
                    }
                    // 新会话创建时已固定版本；迁移前从未使用的空会话在首次受理时绑定。
                    // 历史任务的版本由迁移保留；缺少清单时阻断，不能回退到 current。
                    String releaseHash = db.conversationRelease(conversation);
                    String agentId = db.conversationAgent(conversation);
                    if (releaseHash == null) {
                        if (!"finance_agent".equals(agentId)) {
                            throw new Blocked("release_unavailable");
                        }
                        releaseHash = releases.currentHash();
                        bindRelease(conversation, releaseHash);
                    }
                    Turn t =
                            new Turn(
                                    id(),
                                    conversation,
                                    owner.tenant(),
                                    owner.subject(),
                                    key,
                                    hash,
                                    message,
                                    releaseHash,
                                    "queued",
                                    1,
                                    null,
                                    null,
                                    agentId);
                    var release = releases.requireForTurn(t);
                    // 阶段二：冻结本次执行的发布、记忆和权限版本，随后才创建可领取命令。
                    db.insertTurn(t);
                    memory.admit(t);
                    var grant = governance.lockGrant(conversation);
                    if (grant == null) {
                        throw new Problem(409, "grant_unavailable");
                    }
                    governance.insertPolicy(
                            t.id(),
                            grant.revision(),
                            PayloadJson.canonical(
                                    PayloadJson.scopes(grant.scopes()).stream()
                                            .filter(release.scopes()::contains)
                                            .toList()));
                    db.recordAdmission(t, id());
                    return t.accepted(false);
                });
    }

    private void bindRelease(String conversation, String releaseHash) {
        if (db.bindConversationRelease(conversation, releaseHash) != 1) {
            throw new Problem(409, "conversation_release_conflict");
        }
    }
}
