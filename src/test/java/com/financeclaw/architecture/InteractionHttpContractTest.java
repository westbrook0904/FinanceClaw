package com.financeclaw.architecture;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.api.*;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.app.interaction.port.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.InteractionBinding.Checkpoint;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.interaction.AgentScopeAnswerRules;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.json.JsonCompareMode;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.*;

/** Public answer contracts through the real use case; PostgreSQL tests cover actual atomicity. */
class InteractionHttpContractTest {
    private final Owner owner = new Owner("tenant", "subject");
    private final Turn turn =
            new Turn(
                    "turn",
                    "conversation",
                    "tenant",
                    "subject",
                    "key",
                    "hash",
                    "report",
                    "release",
                    "waiting",
                    3,
                    null,
                    null);
    private final Claim claim =
            new Claim(new Command("command", "turn", "done", "attempt", 1, false, null), turn);
    private final Answer approved = new Answer("approval", 1, "approve", "action", null);
    private final ObjectMapper json =
            new ObjectMapper().setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
    private InteractionGateway gateway;
    private InteractionCheckpoint checkpoints;
    private MockMvc http;

    @BeforeEach
    void setup() {
        gateway = mock(InteractionGateway.class);
        checkpoints = mock(InteractionCheckpoint.class);
        when(gateway.ownedTurn(owner, "conversation", "turn")).thenReturn(turn);
        when(gateway.turn("turn")).thenReturn(turn);
        when(gateway.grantRevision("conversation")).thenReturn(1L);
        configure(interaction("approval", "pending", null));
        when(checkpoints.pending(any()))
                .thenReturn(
                        new Checkpoint(
                                "call", "create_report", "action", true, Map.of("title", "test")));
        // 明确设定完成分支，避免依赖 Mockito 对 boolean 的默认 false。
        when(gateway.existsUnansweredInteractions("turn", "attempt")).thenReturn(false);
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenReturn(new SimpleTransactionStatus());
        var tx = new TransactionTemplate(manager);
        var answers =
                new AnswerInteractionService(
                        gateway, checkpoints, new AgentScopeAnswerRules(List.of()), tx);
        http =
                MockMvcBuilders.standaloneSetup(new GovernanceController(null, null, answers))
                        .setMessageConverters(new MappingJackson2HttpMessageConverter(json))
                        .setControllerAdvice(new ApiErrors())
                        .addFilters(
                                new BearerIdentity(
                                        "contract-test-only", owner.tenant(), owner.subject()))
                        .build();
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "/v1/interactions/question/responses",
                "/v1/conversations/conversation/turns/turn/interactions/question/answer"
            })
    void bothAnswerRoutesKeepAcceptedReceipt(String path) throws Exception {
        http.perform(request(path, approved))
                .andExpect(status().isAccepted())
                .andExpect(
                        content()
                                .json(
                                        "{\"interaction_id\":\"question\",\"turn_id\":\"turn\",\"status\":\"accepted\",\"revision\":1,\"idempotent_replay\":false}",
                                        JsonCompareMode.STRICT));
        var ordered = inOrder(gateway);
        ordered.verify(gateway).answer(eq("question"), anyString(), anyString());
        ordered.verify(gateway).existsUnansweredInteractions("turn", "attempt");
        ordered.verify(gateway).resume(any());
        verify(gateway, never()).interactions(anyString());
        verify(checkpoints).pending(any());
    }

    @Test
    void identicalAnswerReplaysBeforeCurrentPolicyOrCheckpointAndNeverQueuesAgain()
            throws Exception {
        var hash = ExecutionScope.hash(List.of(PayloadJson.canonical(approved)));
        configure(interaction("approval", "answered", hash));
        http.perform(request("/v1/interactions/question/responses", approved))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.idempotent_replay").value(true));
        verifyNoInteractions(checkpoints);
        verify(gateway, never()).checkPolicy(any());
        verify(gateway, never()).answer(any(), any(), any());
        verify(gateway, never()).existsUnansweredInteractions(anyString(), anyString());
        verify(gateway, never()).interactions(anyString());
        verify(gateway, never()).resume(any());
    }

    @Test
    void conflictingReplayKeeps409() throws Exception {
        configure(interaction("approval", "answered", "different-hash"));
        error(approved, 409, "idempotency_conflict");
        verify(gateway, never()).answer(any(), any(), any());
        verify(gateway, never()).existsUnansweredInteractions(anyString(), anyString());
    }

    @Test
    void incompleteBatchDoesNotEnqueueResume() throws Exception {
        when(gateway.existsUnansweredInteractions("turn", "attempt")).thenReturn(true);
        http.perform(request("/v1/interactions/question/responses", approved))
                .andExpect(status().isAccepted());
        var ordered = inOrder(gateway);
        ordered.verify(gateway).answer(eq("question"), anyString(), anyString());
        ordered.verify(gateway).existsUnansweredInteractions("turn", "attempt");
        verify(gateway, never()).interactions(anyString());
        verify(gateway, never()).resume(any());
    }

    @ParameterizedTest
    @CsvSource({
        "input,1,approve,action,409,interaction_mismatch",
        "approval,2,approve,action,409,interaction_mismatch",
        "approval,1,approve,forged,409,interaction_mismatch",
        "approval,1,yes,action,422,invalid_decision",
        "approval,0,approve,action,422,invalid_answer"
    })
    void invalidAnswerKeepsStatusAndError(
            String kind, long revision, String decision, String hash, int status, String code)
            throws Exception {
        error(new Answer(kind, revision, decision, hash, null), status, code);
        verify(gateway, never()).answer(any(), any(), any());
    }

    @ParameterizedTest
    @CsvSource({
        "other,create_report,action,true",
        "call,other,action,true",
        "call,create_report,changed,true",
        "call,create_report,action,false"
    })
    void checkpointMustMatchCallToolHashAndInteractionKind(
            String id, String name, String hash, boolean asking) throws Exception {
        when(checkpoints.pending(any()))
                .thenReturn(new Checkpoint(id, name, hash, asking, Map.of()));
        error(approved, 409, "checkpoint_mismatch");
        verify(gateway, never()).answer(any(), any(), any());
    }

    @Test
    void inputSchemaUsesNativeValidatorAndRejectsInjectedFields() throws Exception {
        configure(interaction("input", "pending", null));
        when(checkpoints.pending(any()))
                .thenReturn(new Checkpoint("call", "create_report", "action", false, Map.of()));
        for (Object value :
                List.of(
                        Map.of("city", 42),
                        Map.of("city", ""),
                        Map.of("city", "上海", "tenant", "forged")))
            error(new Answer("input", 1, null, null, value), 422, "invalid_answer");
        verify(gateway, never()).answer(any(), any(), any());
    }

    @Test
    void authorizationOwnershipAndAuthenticationFailBeforeWriting() throws Exception {
        when(gateway.grantRevision("conversation")).thenReturn(2L);
        error(approved, 409, "authorization_changed");
        when(gateway.ownedTurn(owner, "conversation", "turn"))
                .thenThrow(new Problem(404, "not_found"));
        error(approved, 404, "not_found");
        http.perform(
                        post("/v1/interactions/question/responses")
                                .contentType("application/json")
                                .content(json.writeValueAsString(approved)))
                .andExpect(status().isUnauthorized())
                .andExpect(content().json("{\"error\":\"unauthorized\"}", JsonCompareMode.STRICT));
        verify(gateway, never()).answer(any(), any(), any());
    }

    @Test
    void missingKeyAndOversizedAnswersFailBeforeLocking() throws Exception {
        http.perform(
                        post("/v1/interactions/question/responses")
                                .header("Authorization", "Bearer contract-test-only")
                                .contentType("application/json")
                                .content(json.writeValueAsString(approved)))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(
                        content()
                                .json(
                                        "{\"error\":\"invalid_idempotency_key\"}",
                                        JsonCompareMode.STRICT));
        error(
                new Answer("approval", 1, "approve", "action", null, "x".repeat(2001)),
                422,
                "invalid_answer");
        error(
                new Answer("input", 1, null, null, Map.of("city", "海".repeat(6000))),
                422,
                "invalid_answer");
        verify(gateway, never()).lock(any(), any());
    }

    private void configure(Interaction interaction) {
        when(gateway.find("question")).thenReturn(interaction);
        when(gateway.lock("turn", "question"))
                .thenReturn(new InteractionBinding(claim, interaction));
    }

    private Interaction interaction(String kind, String status, String responseHash) {
        return new Interaction(
                "question",
                "turn",
                "command",
                "attempt",
                4,
                1,
                "release",
                kind,
                "call",
                "create_report",
                "action",
                "{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\",\"minLength\":1}},\"required\":[\"city\"],\"additionalProperties\":false}",
                "question",
                1,
                status,
                true,
                responseHash,
                null);
    }

    private MockHttpServletRequestBuilder request(String path, Answer answer) throws Exception {
        return post(path)
                .header("Authorization", "Bearer contract-test-only")
                .header("Idempotency-Key", "reply")
                .contentType("application/json")
                .content(json.writeValueAsString(answer));
    }

    private void error(Answer answer, int status, String code) throws Exception {
        http.perform(request("/v1/interactions/question/responses", answer))
                .andExpect(status().is(status))
                .andExpect(content().json("{\"error\":\"" + code + "\"}", JsonCompareMode.STRICT));
    }
}
