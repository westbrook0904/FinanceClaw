package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
import io.agentscope.core.message.*;
import java.util.*;
import org.junit.jupiter.api.Test;

/** 故障驱动应容忍 SDK 工作历史压缩，不能重放已成功的工具调用。 */
class ZiweiRuntimeProbeTest {
    @Test
    void fiveToolSequenceAdvancesWhenOnlyTheLatestReceiptRemainsVisible() {
        String[] tools = {
            "ziwei_natal_chart",
            "ziwei_decadal_chart",
            "ziwei_yearly_chart",
            "ziwei_monthly_chart",
            "ziwei_daily_chart"
        };
        var probe = probe(tools);
        assertCall(probe, List.of(), "fixed-0", tools[0]);
        for (int completed = 1; completed < tools.length; completed++) {
            var latest = receipt("fixed-" + (completed - 1), tools[completed - 1], false);
            // 模拟压缩掉较早完整批次；重复观察同一回执不能再次推进进度。
            assertCall(probe, List.of(latest), "fixed-" + completed, tools[completed]);
            assertCall(probe, List.of(latest), "fixed-" + completed, tools[completed]);
        }
        var answer =
                probe.stream(List.of(receipt("fixed-4", tools[4], false)), List.of(), null)
                        .blockFirst();
        assertThat(answer.getContent()).singleElement().isInstanceOf(TextBlock.class);
    }

    @Test
    void aRestoredReceiptSeedsProgressButHistoricalTurnReceiptsDoNot() {
        var probe = probe("ziwei_yearly_chart");
        assertCall(
                probe,
                List.of(receipt("old", "ziwei_yearly_chart", true)),
                "fixed-0",
                "ziwei_yearly_chart");
        var answer =
                probe.stream(
                                List.of(receipt("fixed-0", "ziwei_yearly_chart", false)),
                                List.of(),
                                null)
                        .blockFirst();
        assertThat(answer.getContent()).singleElement().isInstanceOf(TextBlock.class);
    }

    private static ZiweiRuntimeProbe probe(String... tools) {
        var settings = new Properties();
        settings.setProperty("probe.mode", "normal");
        settings.setProperty("probe.tools", String.join(",", tools));
        settings.setProperty("probe.input", "{}");
        return new ZiweiRuntimeProbe(
                null, null, null, null, null, null, null, null, null, settings);
    }

    private static void assertCall(
            ZiweiRuntimeProbe probe, List<Msg> messages, String id, String tool) {
        var response = probe.stream(messages, List.of(), null).blockFirst();
        assertThat(response.getContent())
                .singleElement()
                .isInstanceOfSatisfying(
                        ToolUseBlock.class,
                        call -> {
                            assertThat(call.getId()).isEqualTo(id);
                            assertThat(call.getName()).isEqualTo(tool);
                        });
    }

    private static Msg receipt(String id, String tool, boolean historical) {
        return Msg.builder()
                .role(MsgRole.TOOL)
                .content(ToolResultBlock.text("archived result reference").withIdAndName(id, tool))
                .metadata(
                        historical ? Map.of(WorkingContextSession.HISTORY_MARKER, true) : Map.of())
                .build();
    }
}
