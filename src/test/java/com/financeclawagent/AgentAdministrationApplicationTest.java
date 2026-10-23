package com.financeclawagent;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

class AgentAdministrationApplicationTest {
    private static final String ID = "19a37cb0-fd86-4b59-8bdb-405150f32fe0";
    private static final String HASH = "a".repeat(64);
    private static final Owner OWNER =
            new Owner("tenant-private-sentinel", "owner-private-sentinel");
    @TempDir Path directory;

    @ParameterizedTest
    @MethodSource("invalidArguments")
    void rejectsUnknownRepeatedAndMalformedOptionsBeforeStartingSpring(String[] arguments) {
        assertThatThrownBy(() -> AgentAdministrationApplication.parse(arguments))
                .isInstanceOf(IllegalArgumentException.class);
    }

    static Stream<Arguments> invalidArguments() {
        return Stream.of(
                        new String[] {},
                        new String[] {"unknown"},
                        new String[] {"list", "--tenant", "a"},
                        options("get", "--tenant", "different"),
                        options("get", "--spring.flyway.enabled", "true"),
                        options("get", "--file", "definition.json"),
                        options("create"),
                        options("update", "--file", "definition.json"),
                        options("update", "--file", "definition.json", "--expected-revision", "0"),
                        options("publish", "--draft-revision", "1"),
                        options("publish", "--draft-revision", "0", "--expected-revision", "0"),
                        options("publish", "--draft-revision", "1", "--expected-revision", "-1"),
                        options("publish", "--draft-revision", "1", "--expected-revision", "+1"),
                        options(
                                "publish",
                                "--draft-revision",
                                "1",
                                "--expected-revision",
                                "9223372036854775808"),
                        options("history", "--limit", "0"),
                        options("history", "--limit", "101"),
                        options("history", "--after-revision", "-1"),
                        new String[] {
                            "get", "--tenant", "a", "--subject", "b", "--agent-id", "1-1-1-1-1"
                        },
                        new String[] {"get", "--tenant", "a", "--subject", " ", "--agent-id", ID},
                        new String[] {
                            "create",
                            "--tenant",
                            "a",
                            "--subject",
                            "b",
                            "--agent-id",
                            "finance_agent",
                            "--file",
                            "definition.json"
                        })
                .map(arguments -> Arguments.of((Object) arguments));
    }

    @Test
    void readsOnlyTheTwoEditableFieldsAndKeepsOriginalContentAndFilePermissions() throws Exception {
        String raw = "{\"displayName\":\"  Name  \",\"systemPrompt\":\"  提示词\\n末尾  \"}\n";
        Path path = directory.resolve("definition.json");
        Files.writeString(path, raw);
        var permissions = Files.getPosixFilePermissions(path);
        var input = AgentAdministrationApplication.readDraft(path);
        assertThat(input.displayName()).isEqualTo("  Name  ");
        assertThat(input.systemPrompt()).isEqualTo("  提示词\n末尾  ");
        assertThat(input.inputSha256())
                .isEqualTo(
                        HexFormat.of()
                                .formatHex(
                                        MessageDigest.getInstance("SHA-256")
                                                .digest(raw.getBytes(StandardCharsets.UTF_8))));
        assertThat(Files.getPosixFilePermissions(path)).isEqualTo(permissions);
        assertThat(Files.readString(path)).isEqualTo(raw);
    }

    @Test
    void rejectsUnknownMissingDuplicateTrailingAndNonStringDefinitionFields() throws Exception {
        for (String raw :
                List.of(
                        "[]",
                        "{}",
                        "{\"displayName\":\"name\"}",
                        "{\"displayName\":\"name\",\"systemPrompt\":12}",
                        "{\"displayName\":\"name\",\"systemPrompt\":\"prompt\",\"credentialRef\":\"SECRET\"}",
                        "{\"displayName\":\"name\",\"systemPrompt\":\"a\",\"systemPrompt\":\"b\"}",
                        "{\"displayName\":\"name\",\"systemPrompt\":\"prompt\"} {}",
                        "{\"displayName\":\"name\",\"systemPrompt\":\"   \"}",
                        "{\"displayName\":\"name\",\"systemPrompt\":\"\\u0000\"}",
                        "{\"displayName\":\"name\",\"systemPrompt\":\"\\ud800\"}",
                        PayloadJson.canonical(
                                Map.of("displayName", "n".repeat(201), "systemPrompt", "prompt")),
                        PayloadJson.canonical(
                                Map.of("displayName", "name", "systemPrompt", "中".repeat(5462))))) {
            Path path = directory.resolve("invalid.json");
            Files.writeString(path, raw);
            assertThatThrownBy(() -> AgentAdministrationApplication.readDraft(path))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessage("invalid_agent_definition");
        }
    }

    @Test
    void rejectsMalformedUtf8AndOversizedFilesBeforeParsing() throws Exception {
        Path path = directory.resolve("invalid.json");
        Files.write(path, new byte[] {(byte) 0xc3, 0x28});
        assertThatThrownBy(() -> AgentAdministrationApplication.readDraft(path))
                .isInstanceOf(IllegalArgumentException.class);
        Files.write(path, new byte[AgentAdministrationApplication.MAX_FILE_BYTES + 1]);
        assertThatThrownBy(() -> AgentAdministrationApplication.readDraft(path))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> AgentAdministrationApplication.readDraft(directory))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void createAndGetConfirmContentWithoutExposingDraftOrOwner() {
        var agents = mock(AgentService.class);
        var input =
                new AgentAdministrationApplication.DraftInput(
                        "name-private-sentinel", "prompt-private-sentinel", "b".repeat(64));
        var draft = new AgentDraft(ID, 1, input.displayName(), input.systemPrompt(), HASH);
        when(agents.create(OWNER, ID, input.displayName(), input.systemPrompt())).thenReturn(draft);
        when(agents.get(OWNER, ID)).thenReturn(draft);
        var created =
                AgentAdministrationApplication.dispatch(
                        AgentAdministrationApplication.parse(
                                options("create", "--file", "definition.json")),
                        agents,
                        input);
        var fetched =
                AgentAdministrationApplication.dispatch(
                        AgentAdministrationApplication.parse(options("get")), agents, null);
        var createdJson = PayloadJson.object(created.json());
        var fetchedJson = PayloadJson.object(fetched.json());
        assertThat(createdJson)
                .containsOnlyKeys(
                        "result",
                        "agentId",
                        "draftRevision",
                        "baseReleaseHash",
                        "contentHash",
                        "inputSha256");
        assertThat(createdJson.get("contentHash")).isEqualTo(fetchedJson.get("contentHash"));
        assertThat(createdJson).containsEntry("inputSha256", input.inputSha256());
        for (String output : List.of(created.json(), fetched.json())) {
            assertThat(output)
                    .doesNotContain(
                            input.displayName(),
                            input.systemPrompt(),
                            OWNER.tenant(),
                            OWNER.subject(),
                            "systemPrompt",
                            "displayName");
        }
    }

    @Test
    void updateAndPublishUseOnlyExplicitRevisionsWithoutFetchingLatest() {
        var agents = mock(AgentService.class);
        var input = new AgentAdministrationApplication.DraftInput("name", "prompt", HASH);
        when(agents.update(OWNER, ID, 3, "name", "prompt"))
                .thenReturn(new AgentDraft(ID, 4, "name", "prompt", HASH));
        AgentAdministrationApplication.dispatch(
                AgentAdministrationApplication.parse(
                        options("update", "--file", "definition.json", "--expected-revision", "3")),
                agents,
                input);
        when(agents.publish(OWNER, ID, 4, 0))
                .thenReturn(new Deployment("private-deployment-name", HASH, 1));
        var published =
                AgentAdministrationApplication.dispatch(
                        AgentAdministrationApplication.parse(
                                options(
                                        "publish",
                                        "--draft-revision",
                                        "4",
                                        "--expected-revision",
                                        "0")),
                        agents,
                        null);
        assertThat(PayloadJson.object(published.json()))
                .containsOnlyKeys("result", "agentId", "releaseHash", "revision")
                .containsEntry("revision", 1);
        assertThat(published.json()).doesNotContain("private-deployment-name");
        verify(agents).update(OWNER, ID, 3, "name", "prompt");
        verify(agents).publish(OWNER, ID, 4, 0);
        verifyNoMoreInteractions(agents);
    }

    @Test
    void listAndHistoryExposeOnlySafeMetadataAndAdvanceCursorFromReceipts() {
        var agents = mock(AgentService.class);
        when(agents.list(OWNER))
                .thenReturn(List.of(new AgentDefinition(ID, OWNER.tenant(), OWNER.subject(), 2)));
        var listed =
                AgentAdministrationApplication.dispatch(
                        AgentAdministrationApplication.parse(
                                new String[] {
                                    "list", "--tenant", OWNER.tenant(), "--subject", OWNER.subject()
                                }),
                        agents,
                        null);
        assertThat(listed.json()).contains(ID).doesNotContain(OWNER.tenant(), OWNER.subject());
        when(agents.history(OWNER, ID, 0, 1))
                .thenReturn(List.of(new DeploymentChange("private-deployment", 1, HASH, null)));
        var arguments = AgentAdministrationApplication.parse(options("history", "--limit", "1"));
        var page = AgentAdministrationApplication.dispatch(arguments, agents, null);
        assertThat(PayloadJson.object(page.json())).containsEntry("nextAfterRevision", 1);
        assertThat(page.json()).doesNotContain("private-deployment");
        when(agents.history(OWNER, ID, 0, 1)).thenReturn(List.of());
        var empty = AgentAdministrationApplication.dispatch(arguments, agents, null);
        assertThat(PayloadJson.object(empty.json())).containsEntry("nextAfterRevision", 0);
    }

    @Test
    void onlyAllowlistedBusinessErrorsReachOutput() {
        assertThat(
                        PayloadJson.object(
                                AgentAdministrationApplication.failure(
                                                new Problem(409, "agent_draft_revision_changed"))
                                        .json()))
                .isEqualTo(Map.of("result", "blocked", "error", "agent_draft_revision_changed"));
        for (RuntimeException failure :
                List.of(
                        new IllegalStateException("jdbc-password-private-sentinel"),
                        new Problem(500, "prompt-private-sentinel"),
                        new Blocked("credential-private-sentinel"))) {
            assertThat(PayloadJson.object(AgentAdministrationApplication.failure(failure).json()))
                    .isEqualTo(Map.of("result", "blocked", "error", "agent_admin_failed"));
        }
    }

    private static String[] options(String command, String... extra) {
        var result =
                new ArrayList<>(
                        List.of(
                                command,
                                "--tenant",
                                OWNER.tenant(),
                                "--subject",
                                OWNER.subject(),
                                "--agent-id",
                                ID));
        result.addAll(List.of(extra));
        return result.toArray(String[]::new);
    }
}
