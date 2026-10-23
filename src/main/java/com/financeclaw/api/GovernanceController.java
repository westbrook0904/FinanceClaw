package com.financeclaw.api;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.support.PayloadJson;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import org.springframework.web.bind.annotation.*;

/**
 * 授权、人工交互和归档结果的 HTTP 入口。
 *
 * <p>调用者只能操作认证主体拥有的会话；幂等键和预期修订号由应用服务在事务中核验， 控制器只转换协议对象，不根据客户端字段推断授权。
 */
@RestController
@RequestMapping("/v1")
public class GovernanceController {
    private final GrantService grants;
    private final ArtifactService artifacts;
    private final AnswerInteractionService answers;

    public GovernanceController(
            GrantService grants, ArtifactService artifacts, AnswerInteractionService answers) {
        this.grants = grants;
        this.artifacts = artifacts;
        this.answers = answers;
    }

    public record GrantView(long revision, List<String> scopes) {}

    public record GrantRequest(long revision, List<String> scopes) {}

    public record Question(
            String id,
            String kind,
            String tool,
            String actionHash,
            Object schema,
            String question,
            long revision,
            String status,
            boolean live) {}

    @GetMapping("/conversations/{conversation}/grant")
    public GrantView grant(HttpServletRequest request, @PathVariable String conversation) {
        var g = grants.grant(TaskController.owner(request), conversation);
        if (g == null) {
            throw new Problem(409, "grant_unavailable");
        }
        return new GrantView(g.revision(), PayloadJson.scopes(g.scopes()));
    }

    @PutMapping("/conversations/{conversation}/grant")
    public GrantView change(
            HttpServletRequest request,
            @PathVariable String conversation,
            @RequestHeader(value = "Idempotency-Key", required = false) String key,
            @RequestBody GrantRequest value) {
        var g =
                grants.changeGrant(
                        TaskController.owner(request),
                        conversation,
                        key,
                        value.revision(),
                        value.scopes());
        return new GrantView(g.revision(), PayloadJson.scopes(g.scopes()));
    }

    @GetMapping("/conversations/{conversation}/turns/{turn}/interactions")
    public List<Question> questions(
            HttpServletRequest request,
            @PathVariable String conversation,
            @PathVariable String turn) {
        return answers.interactions(TaskController.owner(request), conversation, turn).stream()
                .map(
                        i ->
                                new Question(
                                        i.id(),
                                        i.kind(),
                                        i.tool(),
                                        i.actionHash(),
                                        PayloadJson.parse(i.schema()),
                                        i.question(),
                                        i.revision(),
                                        i.status(),
                                        i.live()))
                .toList();
    }

    @ResponseStatus(org.springframework.http.HttpStatus.ACCEPTED)
    @PostMapping("/conversations/{conversation}/turns/{turn}/interactions/{interaction}/answer")
    public AnswerReceipt answer(
            HttpServletRequest request,
            @PathVariable String conversation,
            @PathVariable String turn,
            @PathVariable String interaction,
            @RequestHeader(value = "Idempotency-Key", required = false) String key,
            @RequestBody Answer answer) {
        return answers.answer(
                TaskController.owner(request), conversation, turn, interaction, key, answer);
    }

    @GetMapping("/interactions/{interaction}")
    public Question question(HttpServletRequest request, @PathVariable String interaction) {
        var i = answers.ownedInteraction(TaskController.owner(request), interaction);
        return new Question(
                i.id(),
                i.kind(),
                i.tool(),
                i.actionHash(),
                PayloadJson.parse(i.schema()),
                i.question(),
                i.revision(),
                i.status(),
                i.live());
    }

    @ResponseStatus(org.springframework.http.HttpStatus.ACCEPTED)
    @PostMapping("/interactions/{interaction}/responses")
    public AnswerReceipt respond(
            HttpServletRequest request,
            @PathVariable String interaction,
            @RequestHeader(value = "Idempotency-Key", required = false) String key,
            @RequestBody Answer answer) {
        return answers.answer(TaskController.owner(request), interaction, key, answer);
    }

    @GetMapping("/artifacts/{artifact}")
    public Page artifact(
            HttpServletRequest request,
            @PathVariable String artifact,
            @RequestParam(defaultValue = "0") int start,
            @RequestParam(defaultValue = "20") int limit,
            @RequestParam(defaultValue = "") String path) {
        return artifacts.read(TaskController.owner(request), artifact, start, limit, path);
    }
}
