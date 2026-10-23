package com.financeclaw.infrastructure.agentscope.context;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextCompactor.SummaryRequest;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.util.JsonUtils;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

class WorkingContextCompactorTest {
    private static final WorkContext SUMMARY =
            new WorkContext(
                    List.of("核对证据后完成分析"),
                    List.of("已读取历史快照"),
                    List.of("来源及局限保留在工作记录"),
                    List.of("继续处理当前用户请求"));

    @Test
    void belowThresholdDoesNotCallSummaryModelOrMutateMessages() {
        var current = user("当前请求");
        var input = List.of(current);
        var compactor = new WorkingContextCompactor(10000, 1000, 80, 1);
        var result = compactor.compact(input, current.getId(), request -> fail("不应调用摘要模型"));
        assertThat(result).containsExactly(current);
        assertThatThrownBy(() -> result.add(user("extra")))
                .isInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void completeHistoricalQuestionAnswerIsReplacedAsOneUnit() {
        var oldQuestion = user("旧问题".repeat(1000));
        var oldAnswer = assistant("旧回答".repeat(1000));
        var current = user("当前问题必须完整保留");
        var recent = assistant("最近一轮保留");
        var input = List.of(oldQuestion, oldAnswer, current, recent);
        List<SummaryRequest> requests = new ArrayList<>();
        var result = compact(input, current, requests);
        assertThat(requests).hasSize(1);
        assertThat(requests.getFirst().completedMessages()).containsExactly(oldQuestion, oldAnswer);
        assertThat(result.subList(1, result.size())).containsExactly(current, recent);
        assertThat(result.getFirst().getRole()).isEqualTo(MsgRole.USER);
        assertThat(result.getFirst().getName()).isEqualTo(WorkingContextCompactor.SUMMARY_NAME);
        assertThat(WorkingContextCompactor.isSummary(result.getFirst())).isTrue();
        assertThat(WorkContext.fromJson(result.getFirst().getTextContent())).isEqualTo(SUMMARY);
        assertThat(input).containsExactly(oldQuestion, oldAnswer, current, recent);
    }

    @Test
    void oneTurnCanCompactOldToolRoundsWhileRetainingItsUserAnchorAndLatestBatch() {
        var current = user("只处理这份报告");
        var first = call("first", "market_snapshot");
        var second = call("second", "market_snapshot");
        var firstCall = assistant(first);
        var firstResult = tool(success(first, "large".repeat(1000)));
        var secondCall = assistant(second);
        var secondResult = tool(success(second, "recent"));
        var input = List.of(current, firstCall, firstResult, secondCall, secondResult);
        List<SummaryRequest> requests = new ArrayList<>();
        var result = compact(input, current, requests);
        assertThat(requests.getFirst().completedMessages()).containsExactly(firstCall, firstResult);
        assertThat(result.subList(1, result.size()))
                .containsExactly(current, secondCall, secondResult);
    }

    @Test
    void incompleteParallelBatchIsRetainedWithAllItsSuccessfulSiblings() {
        var current = user("当前任务");
        var finished = call("finished", "read");
        var pending = call("pending", "read");
        var parallel = assistant(finished, pending);
        var partial = tool(success(finished, "部分结果"));
        var input = withHistory(current, parallel, partial);
        var result = compact(input, current, new ArrayList<>());
        assertThat(result).contains(current, parallel, partial);
        assertThat(result).doesNotContain(input.getFirst(), input.get(1));
    }

    @Test
    void askingSuspendedRunningAndFailedResultsCannotDisappearIntoSummary() {
        for (int kind = 0; kind < 6; kind++) {
            var current = user("当前任务");
            var original = call("protected-" + kind, "write_report");
            var call = kind == 0 ? original.withState(ToolCallState.ASKING) : original;
            var value =
                    switch (kind) {
                        case 1 -> ToolResultBlock.suspended(call);
                        case 2 -> success(call, "running").withState(ToolResultState.RUNNING);
                        case 3 -> success(call, "error").withState(ToolResultState.ERROR);
                        case 4 -> success(call, "denied").withState(ToolResultState.DENIED);
                        case 5 ->
                                success(call, "interrupted").withState(ToolResultState.INTERRUPTED);
                        default -> success(call, "asking");
                    };
            var callMessage = assistant(call);
            var resultMessage = tool(value);
            var input = withHistory(current, callMessage, resultMessage);
            var result = compact(input, current, new ArrayList<>());
            assertThat(result)
                    .as("protected result kind %s", kind)
                    .contains(callMessage, resultMessage);
        }
    }

    @Test
    void duplicateCallIdsAcrossRoundsAreNotSplitOrDiscarded() {
        var current = user("当前任务");
        var call = call("reused", "read");
        var first = assistant(call);
        var firstResult = tool(success(call, "one"));
        var second = assistant(call);
        var secondResult = tool(success(call, "two"));
        var input = withHistory(current, first, firstResult, second, secondResult);
        var result = compact(input, current, new ArrayList<>());
        assertThat(result).contains(first, firstResult, second, secondResult);
    }

    @Test
    void orphanToolResultsAreRetainedRatherThanReinterpretedAsCompletedRounds() {
        var current = user("当前任务");
        var orphan = tool(success(call("orphan", "read"), "unmatched"));
        var input = withHistory(current, orphan);
        var result = compact(input, current, new ArrayList<>());
        assertThat(result).contains(current, orphan);
    }

    @Test
    void systemAndLongTermMemoryNeverReachSummaryModel() {
        var current = user("当前任务");
        var system = Msg.builder().role(MsgRole.SYSTEM).textContent("PRIVATE_PROFILE").build();
        var compactor = new WorkingContextCompactor(10000, 1000, 80, 1);
        assertThatThrownBy(
                        () ->
                                compactor.compact(
                                        List.of(system, current),
                                        current.getId(),
                                        request -> fail("不能发送长期记忆")))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("long-term memory");
    }

    @Test
    void absentOrNonUserAnchorCannotSilentlyAllowCurrentRequestToBeCompacted() {
        var answer = assistant("not a user");
        var compactor = new WorkingContextCompactor(10000, 1000, 80, 1);
        for (String anchor : List.of("missing", answer.getId())) {
            assertThatThrownBy(
                            () ->
                                    compactor.compact(
                                            List.of(answer),
                                            anchor,
                                            request -> fail("invalid anchor")))
                    .isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    void protectedContentOverBudgetBlocksBeforeAnyModelRequest() {
        var current = user("不能截断".repeat(1000));
        var compactor = new WorkingContextCompactor(1500, 500, 80, 1);
        assertThatThrownBy(
                        () ->
                                compactor.compact(
                                        List.of(current),
                                        current.getId(),
                                        request -> fail("没有可删除的轮次")))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_budget_exceeded");
    }

    @Test
    void summaryOutputUsesUtf8BytesAndRejectsOversizedOrMissingSummaries() {
        var current = user("当前任务");
        var input = withHistory(current);
        var compactor =
                new WorkingContextCompactor(WorkingContextCompactor.bytes(input) - 1, 200, 80, 1);
        assertThatThrownBy(
                        () ->
                                compactor.compact(
                                        input,
                                        current.getId(),
                                        request ->
                                                new WorkContext(
                                                        List.of("汉".repeat(100)),
                                                        List.of(),
                                                        List.of(),
                                                        List.of())))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_compaction_failed");
        assertThatThrownBy(() -> compactor.compact(input, current.getId(), request -> null))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_compaction_failed");
    }

    @Test
    void consecutiveCompactionMergesPreviousSummaryWithoutSummarizingItsUserMessageAgain() {
        var current = user("当前任务");
        var previous = WorkingContextCompactor.summaryMessage(SUMMARY);
        var input = new ArrayList<>(withHistory(current));
        input.addFirst(previous);
        List<SummaryRequest> requests = new ArrayList<>();
        var result = compact(input, current, requests);
        assertThat(requests.getFirst().previousSummary()).isEqualTo(SUMMARY);
        assertThat(requests.getFirst().completedMessages()).doesNotContain(previous, current);
        assertThat(result.stream().filter(WorkingContextCompactor::isSummary)).hasSize(1);
    }

    @Test
    void largeHistoryIsSummarizedInBoundedWholeRoundBatches() {
        var current = user("当前任务");
        List<Msg> input = new ArrayList<>();
        for (int index = 0; index < 6; index++) {
            input.add(user("old-question-" + index));
            input.add(assistant("old-answer-" + index + "x".repeat(800)));
        }
        input.add(current);
        input.add(assistant("recent"));
        List<SummaryRequest> requests = new ArrayList<>();
        var compactor =
                new WorkingContextCompactor(
                        WorkingContextCompactor.bytes(input) - 1, 1000, 80, 1, 2500);
        var result =
                compactor.compact(
                        input,
                        current.getId(),
                        request -> {
                            requests.add(request);
                            assertThat(
                                            JsonUtils.getJsonCodec()
                                                    .toJson(request)
                                                    .getBytes(StandardCharsets.UTF_8)
                                                    .length)
                                    .isLessThanOrEqualTo(2500);
                            assertThat(request.completedMessages().size() % 2).isZero();
                            return SUMMARY;
                        });
        assertThat(requests).hasSizeGreaterThan(1);
        assertThat(requests.subList(1, requests.size()))
                .allSatisfy(request -> assertThat(request.previousSummary()).isEqualTo(SUMMARY));
        assertThat(result).contains(current);
    }

    @Test
    void singleOversizedToolBatchMustBeOffloadedInsteadOfSendingAnUnboundedSummaryRequest() {
        var current = user("当前任务");
        var large = call("large", "read");
        var input =
                List.of(
                        current,
                        assistant(large),
                        tool(success(large, "x".repeat(6000))),
                        assistant("recent"));
        var calls = new AtomicInteger();
        var compactor =
                new WorkingContextCompactor(
                        WorkingContextCompactor.bytes(input) - 1, 1000, 80, 1, 2500);
        assertThatThrownBy(
                        () ->
                                compactor.compact(
                                        input,
                                        current.getId(),
                                        request -> {
                                            calls.incrementAndGet();
                                            return SUMMARY;
                                        }))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_summary_input_exceeded");
        assertThat(calls).hasValue(0);
    }

    @Test
    void artifactReferencesAndReadRangesSurviveEvenIfModelOmitsThem() {
        var current = user("当前任务");
        var artifact =
                ToolUseBlock.builder()
                        .id("artifact-read")
                        .name("read_artifact")
                        .input(
                                Map.of(
                                        "artifact_id",
                                        "file-1",
                                        "path",
                                        "/rows",
                                        "start",
                                        4,
                                        "limit",
                                        2))
                        .build();
        var value =
                success(
                        artifact,
                        "{\"artifactId\":\"file-1\",\"hash\":\"abc123\",\"total\":20,"
                                + "\"start\":4,\"count\":2,\"data\":\""
                                + "x".repeat(3000)
                                + "\"}");
        var input = List.of(current, assistant(artifact), tool(value), assistant("recent"));
        var result = compact(input, current, new ArrayList<>());
        var summary = WorkContext.fromJson(result.getFirst().getTextContent());
        assertThat(summary.evidence())
                .anySatisfy(
                        entry ->
                                assertThat(entry)
                                        .contains(
                                                "file-1",
                                                "\"path\":\"/rows\"",
                                                "\"start\":4",
                                                "\"limit\":2"))
                .anySatisfy(
                        entry ->
                                assertThat(entry)
                                        .contains(
                                                "file-1", "abc123", "\"count\":2", "\"total\":20"));
        assertThat(summary.evidence()).noneMatch(entry -> entry.contains("x".repeat(100)));
    }

    @Test
    void malformedSummaryIsNotTreatedAsOrdinaryConversationText() {
        var current = user("当前任务");
        var invalid =
                WorkingContextCompactor.summaryMessage(SUMMARY)
                        .withContent(List.of(TextBlock.builder().text("{\"goals\":[]}").build()));
        var input = new ArrayList<>(withHistory(current));
        input.addFirst(invalid);
        assertThatThrownBy(() -> compact(input, current, new ArrayList<>()))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("fields");
    }

    @Test
    void manyReferencesUseOneGovernedIndexAndReserveItsSpaceBeforeSummarization() {
        var current = user("当前问题");
        List<Msg> input = new ArrayList<>();
        for (int i = 0; i < 8; i++) {
            var call = call("file-" + i, "read_artifact_range");
            input.add(assistant(call));
            input.add(
                    tool(
                            success(
                                    call,
                                    PayloadJson.canonical(
                                            Map.of(
                                                    "artifact_id",
                                                    "artifact-" + i,
                                                    "hash",
                                                    "a".repeat(64),
                                                    "bytes",
                                                    9000)))));
        }
        input.add(current);
        input.add(assistant("最近回答"));
        List<String> archivedRefs = new ArrayList<>();
        var index = "artifact_ref:{\"artifact_id\":\"index-file\",\"hash\":\"sha\",\"bytes\":1234}";
        var compactor =
                new WorkingContextCompactor(
                        WorkingContextCompactor.bytes(input) - 1,
                        1500,
                        80,
                        1,
                        WorkingContextCompactor.bytes(input) * 2,
                        refs -> {
                            archivedRefs.addAll(refs);
                            return List.of(index);
                        });
        var output =
                compactor.compact(
                        input,
                        current.getId(),
                        request -> {
                            assertThat(request.maxOutputBytes()).isLessThan(1500);
                            return SUMMARY;
                        });
        assertThat(archivedRefs).hasSize(8);
        var summary = WorkContext.fromJson(output.getFirst().getTextContent());
        assertThat(summary.evidence()).contains(index);
        assertThat(summary.evidence()).noneMatch(ref -> ref.contains("artifact-0"));
        assertThat(summary.json().getBytes(StandardCharsets.UTF_8).length)
                .isLessThanOrEqualTo(1500);
    }

    private static List<Msg> compact(List<Msg> input, Msg current, List<SummaryRequest> requests) {
        return new WorkingContextCompactor(WorkingContextCompactor.bytes(input) - 1, 2000, 80, 1)
                .compact(
                        input,
                        current.getId(),
                        request -> {
                            requests.add(request);
                            return SUMMARY;
                        });
    }

    private static List<Msg> withHistory(Msg current, Msg... tail) {
        List<Msg> messages = new ArrayList<>();
        messages.add(user("旧问题".repeat(1000)));
        messages.add(assistant("旧回答".repeat(1000)));
        messages.add(current);
        messages.addAll(List.of(tail));
        messages.add(assistant("最近完整轮"));
        return messages;
    }

    private static Msg user(String text) {
        return Msg.builder().name("user").role(MsgRole.USER).textContent(text).build();
    }

    private static Msg assistant(String text) {
        return Msg.builder().name("assistant").role(MsgRole.ASSISTANT).textContent(text).build();
    }

    private static Msg assistant(ToolUseBlock... calls) {
        return Msg.builder()
                .name("assistant")
                .role(MsgRole.ASSISTANT)
                .content(List.of(calls))
                .build();
    }

    private static ToolUseBlock call(String id, String tool) {
        return ToolUseBlock.builder().id(id).name(tool).input(Map.of()).build();
    }

    private static ToolResultBlock success(ToolUseBlock call, String text) {
        return ToolResultBlock.text(text)
                .withIdAndName(call.getId(), call.getName())
                .withState(ToolResultState.SUCCESS);
    }

    private static Msg tool(ToolResultBlock value) {
        return Msg.builder().name("tool").role(MsgRole.TOOL).content(value).build();
    }
}
