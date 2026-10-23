package com.financeclaw.m0;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.ExecutionScope;
import java.util.List;
import org.junit.jupiter.api.Test;

class ExecutionScopeTest {
    @Test
    void lengthFramingAvoidsAmbiguousIdentityConcatenation() {
        assertThat(ExecutionScope.hash(List.of("a/b", "c")))
                .isNotEqualTo(ExecutionScope.hash(List.of("a", "b/c")));
        var scope =
                new ExecutionScope(
                        "tenant/中文", "__anon__", "../../../session", "finance/child", "r1");
        assertThat(scope.userId()).matches("[0-9a-f]{64}");
        assertThat(scope.sessionId()).matches("[0-9a-f]{64}");
    }

    @Test
    void missingIdentityCannotFallBackToSdkAnonymousSlot() {
        assertThatThrownBy(() -> new ExecutionScope("", "alice", "c", "a", "r"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new ExecutionScope("tenant", null, "c", "a", "r"))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
