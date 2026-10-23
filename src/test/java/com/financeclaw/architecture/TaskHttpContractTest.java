package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.financeclaw.api.*;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.bootstrap.HttpRequestConfiguration;
import com.financeclaw.domain.ExecutionPolicy;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Grant;
import com.financeclaw.domain.Task.*;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;
import org.mockito.ArgumentCaptor;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.json.JsonCompareMode;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.validation.beanvalidation.LocalValidatorFactoryBean;

/** Real HTTP binding and use cases; PostgreSQL integration tests verify transaction atomicity. */
class TaskHttpContractTest {
    private static final String ROOT = "/v1/conversations";
    private static final String SUBMIT = ROOT + "/conversation/turns";
    private static final String CANCEL = SUBMIT + "/turn/cancel";
    private final Owner owner = new Owner("contract-tenant", "contract-subject");
    private final ObjectMapper json =
            new ObjectMapper().setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
    private TaskAdmissionGateway admissions;
    private TaskQueryGateway reads;
    private TaskMutationGateway mutations;
    private ExecutionPolicyGateway policy;
    private ExecutionMemory memory;
    private CommandGateway commands;
    private MockMvc http;
    private LocalValidatorFactoryBean validator;

    @BeforeEach
    void setup() {
        admissions = mock(TaskAdmissionGateway.class);
        reads = mock(TaskQueryGateway.class);
        mutations = mock(TaskMutationGateway.class);
        policy = mock(ExecutionPolicyGateway.class);
        memory = mock(ExecutionMemory.class);
        commands = mock(CommandGateway.class);
        var releases = mock(ExecutionRelease.class);
        when(admissions.lockConversation("conversation", owner)).thenReturn("conversation");
        when(reads.ownedConversation("conversation", owner)).thenReturn("conversation");
        when(releases.currentHash()).thenReturn("release");
        when(admissions.conversationRelease("conversation")).thenReturn("release");
        when(admissions.bindConversationRelease(anyString(), eq("release"))).thenReturn(1);
        when(releases.require("release")).thenReturn(new ExecutionPolicy(List.of(), 4, 4));
        when(policy.lockGrant("conversation")).thenReturn(new Grant("conversation", 1, "[]"));
        Turn cancelled =
                new Turn(
                        "turn",
                        "conversation",
                        owner.tenant(),
                        owner.subject(),
                        "original",
                        "hash",
                        "original",
                        "release",
                        "cancelled",
                        3,
                        null,
                        "cancel_requested");
        when(reads.turn("turn")).thenReturn(cancelled);
        when(reads.ownedTurn(owner, "conversation", "turn")).thenReturn(cancelled);
        when(mutations.lockTurn("turn")).thenReturn(cancelled);
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        var tx = new TransactionTemplate(manager);
        var queries = new TaskQueryService(reads);
        var submission = new SubmitTaskService(admissions, policy, releases, memory, tx);
        var cancellation =
                new CancelTaskService(
                        queries,
                        reads,
                        mutations,
                        commands,
                        new TaskTransitionService(mutations, policy, memory),
                        tx);
        var converter = new MappingJackson2HttpMessageConverter(json);
        new HttpRequestConfiguration().extendMessageConverters(List.of(converter));
        validator = new LocalValidatorFactoryBean();
        validator.afterPropertiesSet();
        http =
                MockMvcBuilders.standaloneSetup(
                                new TaskController(
                                        submission, queries, cancellation, mock(TaskEvents.class)))
                        .setMessageConverters(converter)
                        .setValidator(validator)
                        .setControllerAdvice(new ApiErrors())
                        .addFilters(
                                new BearerIdentity(
                                        "contract-test-only", owner.tenant(), owner.subject()))
                        .build();
    }

    @AfterEach
    void closeValidator() {
        if (validator != null) validator.close();
    }

    @ParameterizedTest
    @ValueSource(strings = {ROOT, SUBMIT, CANCEL})
    void authenticationPrecedesEvenMalformedBodies(String path) throws Exception {
        http.perform(post(path).contentType("application/json").content("{"))
                .andExpect(status().isUnauthorized())
                .andExpect(content().json("{\"error\":\"unauthorized\"}", JsonCompareMode.STRICT));
        verifyNoInteractions(admissions, reads, mutations, policy, memory, commands);
    }

    @ParameterizedTest
    @MethodSource("bodyErrors")
    void bodyBindingDistinguishesShapeSyntaxAndMissingRequiredInput(
            String path, String body, String code) throws Exception {
        error(path, body, null, code);
        verifyNoInteractions(admissions, reads, mutations, policy, memory, commands);
    }

    static Stream<Arguments> bodyErrors() {
        var nodeShapes =
                Stream.of(ROOT, CANCEL)
                        .flatMap(
                                path ->
                                        Stream.of(
                                                        "null",
                                                        "[]",
                                                        "\"text\"",
                                                        "true",
                                                        "42",
                                                        "{\"tenant\":\"injected\"}")
                                                .map(
                                                        body ->
                                                                Arguments.of(
                                                                        path,
                                                                        body,
                                                                        "invalid_body")));
        var typedShapes =
                Stream.of(
                                "{}",
                                "[]",
                                "\"text\"",
                                "true",
                                "42",
                                "{\"message\":null}",
                                "{\"message\":42}",
                                "{\"message\":false}",
                                "{\"message\":[]}",
                                "{\"message\":{}}",
                                "{\"Message\":\"hello\"}",
                                "{\"message\":\"hello\",\"tenant\":\"injected\"}")
                        .map(body -> Arguments.of(SUBMIT, body, "invalid_body"));
        var required =
                Stream.of(
                        Arguments.of(ROOT, "", "invalid_request"),
                        Arguments.of(SUBMIT, "", "invalid_request"),
                        Arguments.of(SUBMIT, "null", "invalid_request"));
        var blankJson =
                Stream.of(ROOT, SUBMIT, CANCEL)
                        .map(path -> Arguments.of(path, " \t\n", "invalid_body"));
        var brokenJson =
                Stream.of(ROOT, SUBMIT, CANCEL)
                        .flatMap(
                                path ->
                                        Stream.of("{", "{\"message\":\"private-input-canary\",")
                                                .map(
                                                        body ->
                                                                Arguments.of(
                                                                        path,
                                                                        body,
                                                                        "invalid_request")));
        return Stream.of(nodeShapes, typedShapes, required, blankJson, brokenJson).flatMap(v -> v);
    }

    @ParameterizedTest
    @MethodSource("invalidKeys")
    void invalidKeyPrecedesInvalidMessageAndOwnership(String key) throws Exception {
        for (String message : List.of(" ", "x".repeat(32001))) {
            error(
                    SUBMIT,
                    json.writeValueAsString(java.util.Map.of("message", message)),
                    key,
                    "invalid_idempotency_key");
        }
        error(CANCEL, "{}", key, "invalid_idempotency_key");
        verifyNoInteractions(admissions, reads, mutations, policy, memory, commands);
    }

    static Stream<String> invalidKeys() {
        return Stream.of(null, "", " ", "\t", "k".repeat(257));
    }

    @ParameterizedTest
    @MethodSource("invalidMessages")
    void invalidMessagePrecedesConversationLookup(String message) throws Exception {
        error(
                SUBMIT,
                json.writeValueAsString(java.util.Map.of("message", message)),
                "valid",
                "invalid_message");
        verifyNoInteractions(admissions, reads, mutations, policy, memory, commands);
    }

    static Stream<String> invalidMessages() {
        return Stream.of("", " \t\n", "\u2003", "x".repeat(32001), "😀".repeat(16001));
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "{\"message\":\"hello\",\"message\":\"last\"}",
                "{\"message\":null,\"message\":\"last\"}"
            })
    void duplicateMessageFieldsAreRejectedWithoutApplicationCalls(String body) throws Exception {
        error(SUBMIT, body, "valid", "invalid_request");
        verifyNoInteractions(admissions, reads, mutations, policy, memory, commands);
    }

    @ParameterizedTest
    @ValueSource(strings = {"{}", "[]", "null"})
    void trailingJsonValuesAreRejectedForEveryTaskEndpoint(String trailing) throws Exception {
        error(ROOT, "{} " + trailing, "valid", "invalid_body");
        error(SUBMIT, "{\"message\":\"hello\"} " + trailing, "valid", "invalid_body");
        error(CANCEL, "{} " + trailing, "valid", "invalid_body");
        verifyNoInteractions(admissions, reads, mutations, policy, memory, commands);
    }

    @Test
    void malformedTrailingInputIsAnInvalidRequest() throws Exception {
        error(SUBMIT, "{\"message\":\"hello\"} @", "valid", "invalid_request");
        verifyNoInteractions(admissions, reads, mutations, policy, memory, commands);
    }

    @ParameterizedTest
    @ValueSource(strings = {"{}"})
    void createAcceptsOneEmptyObjectAndReturnsItsIdentity(String body) throws Exception {
        var response =
                http.perform(request(ROOT, body, null))
                        .andExpect(status().isCreated())
                        .andReturn()
                        .getResponse()
                        .getContentAsString();
        var conversation = ArgumentCaptor.forClass(String.class);
        verify(admissions).createConversation(conversation.capture(), eq(owner));
        assertDoesNotThrow(() -> java.util.UUID.fromString(conversation.getValue()));
        assertEquals(
                json.readTree("{\"conversation_id\":\"" + conversation.getValue() + "\"}"),
                json.readTree(response));
        verify(policy).ensureGrant(conversation.getValue(), "[]");
    }

    @ParameterizedTest
    @ValueSource(strings = {"", "{}"})
    void cancelAcceptsAbsentOrEmptyObjectBodyAndReturnsItsReceipt(String body) throws Exception {
        http.perform(request(CANCEL, body, "cancel"))
                .andExpect(status().isAccepted())
                .andExpect(
                        content()
                                .json(
                                        "{\"conversation_id\":\"conversation\",\"turn_id\":\"turn\",\"status\":\"cancelled\","
                                                + "\"revision\":3,\"idempotent_replay\":false}",
                                        JsonCompareMode.STRICT));
        verify(mutations).recordCancel("turn", "cancel", "cancelled", 3);
    }

    @ParameterizedTest
    @MethodSource("acceptedMessages")
    void submissionPreservesMessageKeyAndReceipt(String message) throws Exception {
        String key = " key with surrounding spaces ";
        var response =
                http.perform(
                                request(
                                        SUBMIT,
                                        json.writeValueAsString(
                                                java.util.Map.of("message", message)),
                                        key))
                        .andExpect(status().isAccepted())
                        .andReturn()
                        .getResponse()
                        .getContentAsString();
        var turn = ArgumentCaptor.forClass(Turn.class);
        verify(admissions).insertTurn(turn.capture());
        var value = turn.getValue();
        assertEquals(message, value.message());
        assertEquals(key, value.idempotencyKey());
        assertEquals(ExecutionScope.hash(List.of(message)), value.requestHash());
        assertEquals(
                json.readTree(
                        "{\"conversation_id\":\"conversation\",\"turn_id\":\""
                                + value.id()
                                + "\",\"status\":\"queued\",\"revision\":1,\"idempotent_replay\":false}"),
                json.readTree(response));
    }

    static Stream<String> acceptedMessages() {
        return Stream.of("  中文 \r\n\t hello  ", "x".repeat(32000), "😀".repeat(16000));
    }

    @Test
    void idempotencyKeyMaximumLengthIsAccepted() throws Exception {
        String key = "k".repeat(256);
        http.perform(request(SUBMIT, "{\"message\":\"hello\"}", key))
                .andExpect(status().isAccepted());
        verify(admissions).replay("conversation", key);
    }

    @Test
    void replayPrecedesAdmissionPauseAndDoesNotQueueAnotherTurn() throws Exception {
        var replay =
                new Turn(
                        "existing",
                        "conversation",
                        owner.tenant(),
                        owner.subject(),
                        "replay",
                        ExecutionScope.hash(List.of("hello")),
                        "hello",
                        "release",
                        "completed",
                        8,
                        "answer",
                        null);
        when(admissions.replay("conversation", "replay")).thenReturn(replay);
        when(admissions.admissionPaused(owner)).thenReturn(true);
        http.perform(request(SUBMIT, "{\"message\":\"hello\"}", "replay"))
                .andExpect(status().isAccepted())
                .andExpect(
                        content()
                                .json(
                                        "{\"conversation_id\":\"conversation\",\"turn_id\":\"existing\",\"status\":\"queued\","
                                                + "\"revision\":1,\"idempotent_replay\":true}",
                                        JsonCompareMode.STRICT));
        verify(admissions, never()).admissionPaused(any());
        verify(admissions, never()).insertTurn(any());
        verifyNoInteractions(memory);
    }

    private MockHttpServletRequestBuilder request(String path, String body, String key) {
        var request =
                post(path)
                        .header("Authorization", "Bearer contract-test-only")
                        .contentType("application/json")
                        .content(body);
        return key == null ? request : request.header("Idempotency-Key", key);
    }

    private void error(String path, String body, String key, String code) throws Exception {
        http.perform(request(path, body, key))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(content().json("{\"error\":\"" + code + "\"}", JsonCompareMode.STRICT));
    }
}
