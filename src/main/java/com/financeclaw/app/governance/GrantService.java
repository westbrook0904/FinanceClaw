package com.financeclaw.app.governance;

import com.financeclaw.app.governance.port.GrantGateway;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Grant;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.PayloadJson;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 会话权限管理：以权限版本做并发控制，幂等回执保留原始结果；撤权与执行检查通过同一授权行锁串行。 */
@Service
public class GrantService {
    private final TaskQueryService queries;
    private final GrantGateway db;
    private final TransactionTemplate tx;
    private final TaskEventHub events;

    public GrantService(
            TaskQueryService queries,
            GrantGateway db,
            TransactionTemplate tx,
            TaskEventHub events) {
        this.events = events;
        this.queries = queries;
        this.db = db;
        this.tx = tx;
    }

    public Grant grant(Owner owner, String conversation) {
        queries.owned(owner, conversation);
        return db.grant(conversation);
    }

    /** 对权限修改执行乐观版本检查和幂等校验；持有授权排他锁直至回执提交，确保并发执行看到明确的撤权顺序。 */
    public Grant changeGrant(
            Owner owner, String conversation, String key, long revision, List<String> scopes) {
        new IdempotencyKey(key);
        queries.owned(owner, conversation);
        if (scopes == null || !ReleaseDefinition.SCOPES.containsAll(scopes)) {
            throw new Problem(422, "invalid_scopes");
        }
        String json = PayloadJson.canonical(scopes.stream().distinct().sorted().toList());
        String hash = ExecutionScope.hash(List.of(Long.toString(revision), json));
        return tx.execute(
                s -> {
                    var grant = db.lockGrant(conversation);
                    var old = db.grantReceipt(conversation, key);
                    if (old != null) {
                        if (!old.requestHash().equals(hash)) {
                            throw new Problem(409, "idempotency_conflict");
                        }
                        return old.grant();
                    }
                    if (grant == null || grant.revision() != revision) {
                        throw new Problem(409, "stale_grant");
                    }
                    db.updateGrant(conversation, json);
                    events.invalidateConversation(conversation);
                    db.grantChange(conversation, key, hash);
                    return db.grant(conversation);
                });
    }
}
