package com.financeclaw.application;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;

/**
 * Compatibility facade for legacy business tools and channels; all task transactions live in app.
 */
@Service
public class TaskService {
  private final SubmitTaskService submission;
  private final TaskQueryService queries;
  private final CancelTaskService cancellation;
  private final ExecutionLeaseService leases;
  private final ExecutionPolicyService policy;
  private final ExecutionCompletionService completion;
  private final RecoverTaskService recovery;
  private final ProjectTaskService projection;
  private final CapabilityStepService steps;
  private final ExecutionContextGateway context;
  private final ReleaseCatalog catalog;

  public TaskService(
      SubmitTaskService submission,
      TaskQueryService queries,
      CancelTaskService cancellation,
      ExecutionLeaseService leases,
      ExecutionPolicyService policy,
      ExecutionCompletionService completion,
      RecoverTaskService recovery,
      ProjectTaskService projection,
      CapabilityStepService steps,
      ExecutionContextGateway context,
      ReleaseCatalog catalog) {
    this.submission = submission;
    this.queries = queries;
    this.cancellation = cancellation;
    this.leases = leases;
    this.policy = policy;
    this.completion = completion;
    this.recovery = recovery;
    this.projection = projection;
    this.steps = steps;
    this.context = context;
    this.catalog = catalog;
  }

  public String createConversation(Owner owner) {
    return submission.createConversation(owner);
  }

  public Accepted submit(Owner owner, String conversation, String key, String message) {
    return submission.submit(owner, conversation, key, message);
  }

  public void owned(Owner owner, String conversation) {
    queries.owned(owner, conversation);
  }

  public Turn get(Owner owner, String conversation, String turn) {
    return queries.get(owner, conversation, turn);
  }

  public List<Journal> messages(Owner owner, String conversation, long after, int limit) {
    return queries.messages(owner, conversation, after, limit);
  }

  public List<Event> events(Owner owner, String conversation, String turn, long after) {
    return queries.events(owner, conversation, turn, after);
  }

  public Accepted cancel(Owner owner, String conversation, String turn, String key) {
    return cancellation.cancel(owner, conversation, turn, key);
  }

  public Claim claim(String worker, long leaseMillis) {
    return leases.claim(worker, leaseMillis);
  }

  public <T> T fenced(Claim claim, Supplier<T> action) {
    return leases.fenced(claim, action);
  }

  public boolean heartbeat(Claim claim, long leaseMillis) {
    return leases.heartbeat(claim, leaseMillis);
  }

  public String memoryContext(Claim claim) {
    return policy.memoryContext(claim);
  }

  public void checkDispatch(Claim claim) {
    policy.checkDispatch(claim);
  }

  public Policy checkPolicy(Claim claim) {
    return policy.checkPolicy(claim);
  }

  public List<Turn> history(Claim claim) {
    return policy.history(claim);
  }

  public ExecutionIdentity identity(Claim claim) {
    return policy.identity(claim);
  }

  public void checkArtifactPrivacy(Turn turn) {
    policy.checkArtifactPrivacy(turn);
  }

  public void consume(Claim claim, boolean model) {
    policy.consume(claim, model);
  }

  public void progress(Claim claim, String kind, String text, String tool) {
    policy.progress(claim, kind, text, tool);
  }

  public void block(Claim claim, String reason) {
    completion.block(claim, reason);
  }

  public void seal(Claim claim, Outcome outcome) {
    completion.seal(claim, outcome);
  }

  public void finish(Claim claim) {
    completion.finish(claim);
  }

  public boolean acknowledgeCancel(Claim claim) {
    return completion.acknowledgeCancel(claim);
  }

  public void fail(Claim claim) {
    completion.fail(claim);
  }

  public int recoverExpired() {
    return recovery.recoverExpired();
  }

  public boolean projectNext() {
    return projection.projectNext();
  }

  public ReleaseCatalog.Release release(String hash) {
    return catalog.require(hash);
  }

  public String modelApiKey() {
    return catalog.apiKey();
  }

  public String credential(String ref) {
    return catalog.credential(ref);
  }

  public List<String> scopes(Claim claim) {
    return policy.scopes(claim);
  }

  public java.time.Instant requestClock(Claim claim) {
    return context.requestClock(claim.turn().id());
  }

  public Interaction interaction(String id) {
    return context.interaction(id);
  }

  public List<Interaction> interactions(Claim claim) {
    return context.interactions(claim.turn().id());
  }

  public Object localStep(
      Claim claim, String namespace, String step, Object input, Supplier<Object> work) {
    return steps.run(claim, namespace, step, input, work);
  }
}
