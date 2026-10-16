package com.financeclaw.infrastructure.interaction;

import com.financeclaw.application.TaskService;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.ziwei.*;
import org.springframework.stereotype.Component;

@Component
public class ZiweiAnswerValidator implements BusinessAnswerValidator {
  private final TaskService tasks;
  private final GovernanceMapper db;

  public ZiweiAnswerValidator(TaskService tasks, GovernanceMapper db) {
    this.tasks = tasks;
    this.db = db;
  }

  public boolean supports(String tool) {
    return ZiweiInputs.supports(tool);
  }

  public void validate(InteractionBinding binding, Checkpoint checkpoint, Object answer) {
    var turn = binding.claim().turn();
    var policy = tasks.release(turn.releaseHash()).business().ziwei();
    if (policy == null) throw new Problem(409, "ziwei_disabled");
    try {
      var input = ZiweiInputs.corrected(checkpoint.tool(), checkpoint.input(), answer);
      if (!ZiweiInputs.inspect(
              checkpoint.tool(),
              input,
              new ZiweiDates.QueryClock(db.requestClock(turn.id()), policy.queryTimezone()))
          .complete()) throw new Problem(422, "ziwei_input_incomplete");
    } catch (IllegalArgumentException error) {
      throw new Problem(422, "invalid_answer");
    }
  }
}
