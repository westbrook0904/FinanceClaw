package com.financeclaw.app.interaction;

import com.financeclaw.app.interaction.port.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 交互回答入口：校验问题版本、授权快照和运行时检查点后，在一个事务中记录回答并安排恢复执行。 */
@Service
public class AnswerInteractionService {
    private static final int MAX_REASON_CHARS = 2_000;
    private static final int MAX_ANSWER_BYTES = 16_384;
    private final InteractionGateway interactions;
    private final InteractionCheckpoint checkpoints;
    private final InteractionAnswerRules rules;
    private final TransactionTemplate tx;

    public AnswerInteractionService(
            InteractionGateway interactions,
            InteractionCheckpoint checkpoints,
            InteractionAnswerRules rules,
            TransactionTemplate tx) {
        this.interactions = interactions;
        this.checkpoints = checkpoints;
        this.rules = rules;
        this.tx = tx;
    }

    public Interaction ownedInteraction(Owner owner, String id) {
        var interaction = interactions.find(id);
        if (interaction == null) {
            throw new Problem(404, "not_found");
        }
        var turn = interactions.turn(interaction.turnId());
        interactions.ownedTurn(owner, turn.conversationId(), turn.id());
        return interaction;
    }

    public List<Interaction> interactions(Owner owner, String conversation, String turn) {
        interactions.ownedTurn(owner, conversation, turn);
        return interactions.interactions(turn);
    }

    public AnswerReceipt answer(Owner owner, String id, String key, Answer answer) {
        var interaction = ownedInteraction(owner, id);
        var turn = interactions.turn(interaction.turnId());
        return answer(owner, turn.conversationId(), turn.id(), id, key, answer);
    }

    /** 在短事务中完成问题版本、授权、检查点及内容验证；仅当同一暂停批次全部回答完毕时才安排恢复命令。 */
    public AnswerReceipt answer(
            Owner owner, String conversation, String turn, String id, String key, Answer answer) {
        new IdempotencyKey(key);
        interactions.ownedTurn(owner, conversation, turn);
        if (answer == null
                || answer.revision() < 1
                || (answer.reason() != null && answer.reason().length() > MAX_REASON_CHARS)) {
            throw new Problem(422, "invalid_answer");
        }
        String json = PayloadJson.canonical(answer);
        if (json.getBytes(StandardCharsets.UTF_8).length > MAX_ANSWER_BYTES) {
            throw new Problem(422, "invalid_answer");
        }
        String hash = ExecutionScope.hash(List.of(json));
        return tx.execute(
                status -> {
                    // 阶段一：锁定原交互绑定；相同内容可幂等回放，过期版本不得恢复执行。
                    var binding = interactions.lock(turn, id);
                    var replay = binding.replay(hash);
                    if (replay != null) {
                        return replay;
                    }
                    binding.requirePending();
                    try {
                        interactions.checkPolicy(binding.claim());
                    } catch (Blocked error) {
                        throw new Problem(409, error.getMessage());
                    }
                    binding.requireGrant(interactions.grantRevision(conversation));
                    binding.requireAnswer(answer);
                    // 阶段二：以暂停时的检查点契约验证回答，不能改用当前工具定义。
                    var saved = checkpoints.pending(binding);
                    binding.requireCheckpoint(saved);
                    binding.requireContent(answer);
                    if (!binding.interaction().kind().equals("approval")) {
                        rules.validate(binding, saved, answer);
                    }
                    // 阶段三：回答和恢复命令同事务提交，避免已回答但永远无法恢复的中间状态。
                    interactions.answer(id, hash, json);
                    // 写入后仍用独立 SQL 检查同一 Turn/attempt，读取本事务刚保存的回答。
                    // pending（含过期行）和 invalid 都不能触发恢复；不再映射整个 Turn 的历史回答正文。
                    boolean complete =
                            !interactions.existsUnansweredInteractions(
                                    turn, binding.interaction().attemptId());
                    if (complete) {
                        interactions.resume(binding);
                    }
                    return binding.receipt(false);
                });
    }
}
