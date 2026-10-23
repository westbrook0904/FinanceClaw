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

    void resume(InteractionBinding binding);
}
