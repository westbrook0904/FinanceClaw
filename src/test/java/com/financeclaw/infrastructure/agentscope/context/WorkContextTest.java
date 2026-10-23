package com.financeclaw.infrastructure.agentscope.context;

import static org.assertj.core.api.Assertions.*;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import org.junit.jupiter.api.Test;

class WorkContextTest {
    @Test
    void structuredSummaryRoundTripsAndCopiesInputLists() {
        var goals = new ArrayList<>(List.of("目标"));
        var summary = new WorkContext(goals, List.of("工具"), List.of("证据"), List.of("下一步"));
        goals.clear();
        assertThat(summary.goals()).containsExactly("目标");
        assertThat(WorkContext.fromJson(summary.json())).isEqualTo(summary);
        assertThat(WorkContext.fromJson(WorkContext.empty().json())).isEqualTo(WorkContext.empty());
        assertThatThrownBy(() -> summary.goals().add("extra"))
                .isInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void unknownFieldsAndNonStringItemsAreRejected() {
        assertThatThrownBy(
                        () ->
                                WorkContext.fromJson(
                                        "{\"goals\":[],\"tools\":[],\"evidence\":[],\"nextSteps\":[],\"instructions\":[]}"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(
                        () ->
                                WorkContext.fromJson(
                                        "{\"goals\":[4],\"tools\":[],\"evidence\":[],\"nextSteps\":[]}"))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void summaryFieldsHaveIndependentItemLimitsBeforeTheTotalByteBudget() {
        assertThatThrownBy(() -> new WorkContext(null, List.of(), List.of(), List.of()))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new WorkContext(List.of(" "), List.of(), List.of(), List.of()))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(
                        () ->
                                new WorkContext(
                                        Collections.nCopies(33, "goal"),
                                        List.of(),
                                        List.of(),
                                        List.of()))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(
                        () ->
                                new WorkContext(
                                        List.of("x".repeat(8193)), List.of(), List.of(), List.of()))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
