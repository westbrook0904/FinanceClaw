package com.financeclaw.app.scheduling.port;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.scheduling.TriggerFiring;
import com.financeclaw.domain.scheduling.TriggerRule;
import java.time.Instant;
import java.util.List;

public interface TriggerRepository {
    Instant now();

    TriggerRule owned(Owner owner, String id, boolean lock);

    TriggerRule nextDue();

    List<TriggerRule> list(Owner owner, String after, int limit);

    void insert(TriggerRule rule);

    boolean replace(TriggerRule rule, long expected);

    void advance(String id, long revision, Instant next);

    void defer(String id, long revision, Instant occurrence, String reason);

    TriggerFiring replay(String ruleId, String eventKey);

    boolean active(String ruleId);

    void record(TriggerFiring receipt);

    void priority(String turnId, int priority);

    String release(String turnId);

    List<TriggerFiring> history(String ruleId, Instant after, String afterKey, int limit);
}
