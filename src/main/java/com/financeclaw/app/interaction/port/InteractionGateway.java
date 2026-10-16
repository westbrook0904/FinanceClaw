package com.financeclaw.app.interaction.port;

import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.Task.*;
import java.util.List;

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
