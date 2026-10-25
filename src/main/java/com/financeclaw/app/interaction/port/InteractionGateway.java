package com.financeclaw.app.interaction.port;

import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.Task.*;
import java.util.List;

/** 人工交互的业务存储边界：问题、回答和恢复命令使用同一事务，版本与归属校验不能跳过。 */
public interface InteractionGateway {
    Turn ownedTurn(Owner owner, String conversation, String turn);

    Interaction find(String id);

    Turn turn(String id);

    InteractionBinding lock(String turn, String interaction);

    void checkPolicy(Claim claim);

    long grantRevision(String conversation);

    void answer(String id, String hash, String json);

    List<Interaction> interactions(String turn);

    /**
     * 回答写入后，在同一持锁事务中判断指定 Turn/attempt 是否仍有非 answered 交互。 pending（包括过期行）与 invalid 均阻止恢复；空集合返回
     * false，保留原 allMatch 的空集语义。 此探测不替代问题归属、授权、检查点或有效期校验，也不替代原生恢复所需的完整历史读取。
     */
    boolean existsUnansweredInteractions(String turn, String attempt);

    void resume(InteractionBinding binding);
}
