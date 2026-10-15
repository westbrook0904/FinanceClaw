package com.financeclaw.channel;

import com.fasterxml.jackson.databind.JsonNode;
import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.application.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Feishu.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import io.agentscope.extensions.channel.feishu.FeishuInboundMapper;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Native inbound mapping followed by the existing business admission/interaction APIs. */
@Service
public class FeishuService {
  private final TaskService tasks;
  private final GovernanceService governance;
  private final FeishuMapper db;
  private final TransactionTemplate tx;
  private final FeishuSettings settings;

  public FeishuService(
      TaskService tasks,
      GovernanceService governance,
      FeishuMapper db,
      TransactionTemplate tx,
      FeishuSettings settings) {
    this.tasks = tasks;
    this.governance = governance;
    this.db = db;
    this.tx = tx;
    this.settings = settings;
  }

  public String link(Owner owner, String conversation) {
    if (!settings.enabled) throw new Problem(404, "not_found");
    tasks.owned(owner, conversation);
    String code = UUID.randomUUID().toString().replace("-", "");
    db.createLink(hash(code), owner, conversation);
    return code;
  }

  private static String hash(String text) {
    return ExecutionScope.hash(List.of(text));
  }

  private static String required(JsonNode n, String field) {
    String value = n.path(field).asText();
    if (value.isBlank() || value.length() > 256) throw new Problem(422, "invalid_callback");
    return value;
  }

  public Object receive(JsonNode root) {
    var challenge = FeishuInboundMapper.extractUrlChallenge(root);
    if (challenge.isPresent()) return Map.of("challenge", challenge.get());
    String event =
        FeishuInboundMapper.extractEventId(root)
            .orElseThrow(() -> new Problem(422, "missing_event_id"));
    if (event.length() > 256) throw new Problem(422, "invalid_callback");
    if (!root.path("header").path("event_type").asText().equals("im.message.receive_v1"))
      return Map.of("code", 0);
    JsonNode body = root.path("event");
    if (!body.path("message").path("chat_type").asText().equals("p2p")
        || !body.path("sender").path("sender_type").asText().equals("user"))
      throw new Problem(403, "direct_user_messages_only");
    var inbound =
        new FeishuInboundMapper("financeclaw")
            .map(root)
            .orElseThrow(() -> new Problem(422, "text_required"));
    String user = required(body.path("sender").path("sender_id"), "open_id");
    String chat = inbound.peer().id();
    String requestHash = hash(ResumeInputs.canonical(GovernanceService.parse(root.toString())));
    String text = inbound.messages().getFirst().getTextContent();
    return tx.execute(
        s -> {
          var binding = db.find(settings.tenantKey, user, chat);
          var old = db.inbox(event);
          if (old != null) {
            if (!old.requestHash().equals(requestHash)) throw new Problem(409, "event_conflict");
            return GovernanceService.parse(old.receipt());
          }
          if (text.startsWith("/bind ")) {
            String code = text.substring(6).trim();
            var link = db.link(hash(code));
            if (link == null || !link.live()) throw new Problem(403, "invalid_binding_code");
            if (binding != null
                && (!binding.tenant().equals(link.tenant())
                    || !binding.subject().equals(link.subject())))
              throw new Problem(409, "identity_already_bound");
            if (binding == null) {
              binding =
                  new Binding(
                      UUID.randomUUID().toString(),
                      settings.tenantKey,
                      user,
                      chat,
                      link.tenant(),
                      link.subject(),
                      link.conversationId(),
                      1,
                      false,
                      null);
              db.bind(binding);
            } else db.switchConversation(binding.id(), link.conversationId());
            db.consumeLink(hash(code));
          } else {
            if (binding == null) throw new Problem(403, "identity_not_bound");
            Owner owner = new Owner(binding.tenant(), binding.subject());
            if (text.equals("/mute") || text.equals("/unmute"))
              db.mute(binding.id(), text.equals("/mute"));
            else if (text.equals("/new"))
              db.switchConversation(binding.id(), tasks.createConversation(owner));
            else if (text.equals("/cancel")) {
              if (binding.activeTurn() == null) throw new Problem(409, "no_active_turn");
              tasks.cancel(
                  owner, binding.conversationId(), binding.activeTurn(), "feishu:" + event);
            } else if (text.startsWith("/approve ") || text.startsWith("/reject ")) {
              if (binding.muted() || binding.activeTurn() == null)
                throw new Problem(409, "stale_approval");
              var turn = tasks.get(owner, binding.conversationId(), binding.activeTurn());
              String[] command = text.split(" ", 2);
              var current = binding;
              var interaction =
                  governance.interactions(owner, binding.conversationId(), turn.id()).stream()
                      .filter(
                          i ->
                              i.kind().equals("approval")
                                  && i.live()
                                  && i.status().equals("pending")
                                  && FeishuText.token(current, turn, i).equals(command[1]))
                      .findFirst()
                      .orElseThrow(() -> new Problem(409, "stale_approval"));
              governance.answer(
                  owner,
                  binding.conversationId(),
                  turn.id(),
                  interaction.id(),
                  "feishu:" + event,
                  new Answer(
                      "approval",
                      interaction.revision(),
                      command[0].equals("/approve") ? "approve" : "reject",
                      interaction.actionHash(),
                      null));
            } else {
              var pending =
                  binding.activeTurn() == null
                      ? List.<Interaction>of()
                      : governance
                          .interactions(owner, binding.conversationId(), binding.activeTurn())
                          .stream()
                          .filter(i -> i.live() && i.status().equals("pending"))
                          .toList();
              if (pending.size() == 1 && pending.getFirst().kind().equals("input")) {
                var i = pending.getFirst();
                governance.answer(
                    owner,
                    binding.conversationId(),
                    binding.activeTurn(),
                    i.id(),
                    "feishu:" + event,
                    new Answer(i.kind(), i.revision(), null, null, FeishuText.answer(i, text)));
              } else {
                var turn =
                    tasks.submit(
                        owner,
                        binding.conversationId(),
                        "feishu:" + required(body.path("message"), "message_id"),
                        text);
                db.active(binding.id(), turn.turnId());
              }
            }
          }
          Object result = Map.of("code", 0);
          db.recordInbox(event, binding.id(), requestHash, ResumeInputs.canonical(result));
          return result;
        });
  }
}
