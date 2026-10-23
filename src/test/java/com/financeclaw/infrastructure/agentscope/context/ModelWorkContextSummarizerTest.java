package com.financeclaw.infrastructure.agentscope.context;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.domain.Governance.Blocked;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import reactor.core.publisher.Flux;

class ModelWorkContextSummarizerTest {
    private final Model model = mock(Model.class);

    @Test
    void qwenSummaryDisablesThinkingPerRequestWithoutChangingThePrimaryModelDefaults() {
        when(model.getModelName()).thenReturn("Qwen3.5-plus");
        when(model.stream(anyList(), anyList(), any()))
                .thenReturn(response(WorkContext.empty().json()));
        new ModelWorkContextSummarizer(model, 1024, Duration.ofSeconds(2), false)
                .summarize(request());
        ArgumentCaptor<GenerateOptions> options = ArgumentCaptor.forClass(GenerateOptions.class);
        verify(model).stream(anyList(), eq(List.of()), options.capture());
        var primary =
                GenerateOptions.builder().additionalBodyParam("enable_thinking", true).build();
        assertThat(
                        GenerateOptions.mergeOptions(options.getValue(), primary)
                                .getAdditionalBodyParams())
                .containsEntry("enable_thinking", false);
        assertThat(primary.getAdditionalBodyParams()).containsEntry("enable_thinking", true);
    }

    @Test
    void realSummaryRequestHasNoToolsAndMergesPreviousStructuredWorkData() {
        var summary =
                new WorkContext(
                        List.of("用户目标"),
                        List.of("search:success"),
                        List.of("检索证据"),
                        List.of("核对来源"));
        when(model.stream(anyList(), anyList(), any())).thenReturn(response(summary.json()));
        var request = request();

        assertThat(
                        new ModelWorkContextSummarizer(model, 1024, Duration.ofSeconds(2), false)
                                .summarize(request))
                .isEqualTo(summary);

        @SuppressWarnings("unchecked")
        ArgumentCaptor<List<Msg>> messages = ArgumentCaptor.forClass(List.class);
        ArgumentCaptor<GenerateOptions> options = ArgumentCaptor.forClass(GenerateOptions.class);
        verify(model).stream(messages.capture(), eq(List.of()), options.capture());
        assertThat(messages.getValue()).hasSize(2);
        assertThat(messages.getValue().getFirst().getRole()).isEqualTo(MsgRole.SYSTEM);
        assertThat(messages.getValue().getLast().getTextContent())
                .contains(
                        "previous goal",
                        "completed question",
                        "previousSummary",
                        "completedMessages");
        assertThat(options.getValue().getMaxTokens()).isEqualTo(1024);
    }

    @Test
    void unstructuredOversizedOrToolCallingSummaryCannotEraseHistoricalContext() {
        var summarizer = new ModelWorkContextSummarizer(model, 1024, Duration.ofSeconds(2), false);
        when(model.stream(anyList(), anyList(), any())).thenReturn(response("not JSON"));
        assertThatThrownBy(() -> summarizer.summarize(request())).isInstanceOf(Blocked.class);
        when(model.stream(anyList(), anyList(), any())).thenReturn(response("中".repeat(1000)));
        assertThatThrownBy(() -> summarizer.summarize(request()))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_summary_output_exceeded");
        when(model.stream(anyList(), anyList(), any()))
                .thenReturn(
                        Flux.just(
                                ChatResponse.builder()
                                        .content(
                                                List.of(
                                                        ToolUseBlock.builder()
                                                                .id("bad")
                                                                .name("write_profile")
                                                                .input(Map.of())
                                                                .content("{}")
                                                                .build()))
                                        .build()));
        assertThatThrownBy(() -> summarizer.summarize(request()))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_summary_invalid");
    }

    @Test
    void offlineSummaryMakesNoProviderCallAndRetainsBoundedSourceExcerpts() {
        var result =
                new ModelWorkContextSummarizer(model, 1024, Duration.ofSeconds(2), true)
                        .summarize(request());
        assertThat(result.goals()).contains("previous goal", "completed question");
        assertThat(result.evidence()).contains("completed answer");
        assertThat(ContextBudgets.bytes(result.json())).isLessThanOrEqualTo(2048);
        verifyNoInteractions(model);
    }

    private static WorkingContextCompactor.SummaryRequest request() {
        return new WorkingContextCompactor.SummaryRequest(
                new WorkContext(List.of("previous goal"), List.of(), List.of(), List.of()),
                List.of(
                        Msg.builder().role(MsgRole.USER).textContent("completed question").build(),
                        Msg.builder()
                                .role(MsgRole.ASSISTANT)
                                .textContent("completed answer")
                                .build()),
                2048);
    }

    private static Flux<ChatResponse> response(String text) {
        return Flux.just(
                ChatResponse.builder()
                        .content(List.of(TextBlock.builder().text(text).build()))
                        .build());
    }
}
