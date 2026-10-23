package com.financeclawrelease;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.PayloadJson;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

class ReleaseAdministrationApplicationTest {
    private static final String HASH = "a".repeat(64);

    @ParameterizedTest
    @MethodSource("invalidArguments")
    void rejectsUnknownRepeatedAndMalformedArgumentsBeforeStartingSpring(String[] arguments) {
        assertThatThrownBy(() -> ReleaseAdministrationApplication.parse(arguments))
                .isInstanceOf(IllegalArgumentException.class);
    }

    static Stream<Arguments> invalidArguments() {
        return Stream.of(
                        new String[] {},
                        new String[] {"unknown"},
                        new String[] {"prepare", "--spring.flyway.enabled=true"},
                        new String[] {"prepare", "--hash", HASH},
                        new String[] {"activate"},
                        new String[] {"activate", "--hash", HASH},
                        new String[] {
                            "activate", "--hash", "not-a-hash", "--expected-revision", "1"
                        },
                        new String[] {"activate", "--hash", HASH, "--expected-revision", "0"},
                        new String[] {"activate", "--hash", HASH, "--expected-revision", "+1"},
                        new String[] {
                            "activate", "--hash", HASH, "--expected-revision", "9223372036854775808"
                        },
                        new String[] {"history", "--limit", "0"},
                        new String[] {"history", "--limit", "101"},
                        new String[] {"history", "--limit", "20", "--limit", "20"},
                        new String[] {"history", "--after-revision", "-1"},
                        new String[] {"history", "--spring.main.web-application-type", "servlet"})
                .map(arguments -> Arguments.of((Object) arguments));
    }

    @Test
    void prepareReturnsOnlyCandidateAndCurrentWithoutActivating() {
        var releases = mock(ReleaseService.class);
        var source = mock(ReleaseSource.class);
        var definition = mock(ReleaseDefinition.class);
        when(source.initial()).thenReturn(definition);
        when(releases.register(definition)).thenReturn(HASH);
        when(releases.current()).thenReturn(new Deployment("default", "b".repeat(64), 4));

        var response =
                ReleaseAdministrationApplication.dispatch(
                        ReleaseAdministrationApplication.parse(new String[] {"prepare"}),
                        releases,
                        source);

        assertThat(response.status()).isZero();
        assertThat(PayloadJson.object(response.json()))
                .containsOnlyKeys("result", "candidateHash", "current")
                .containsEntry("candidateHash", HASH);
        verify(releases, never()).activate(anyString(), anyLong());
        verify(releases, never()).publish(any());
    }

    @Test
    void activateUsesExactlyTheOperatorsRevisionAndDoesNotFetchLatestToRetry() {
        var releases = mock(ReleaseService.class);
        var source = mock(ReleaseSource.class);
        when(releases.activate(HASH, 7)).thenReturn(new Deployment("default", HASH, 8));

        var response =
                ReleaseAdministrationApplication.dispatch(
                        ReleaseAdministrationApplication.parse(
                                new String[] {
                                    "activate", "--hash", HASH, "--expected-revision", "7"
                                }),
                        releases,
                        source);

        assertThat(PayloadJson.object(response.json()))
                .containsOnlyKeys("result", "current")
                .containsEntry("result", "activated");
        verify(releases).activate(HASH, 7);
        verifyNoMoreInteractions(releases);
        verifyNoInteractions(source);
    }

    @Test
    void historyCursorComesFromReturnedReceiptAndEmptyPageKeepsCursor() {
        var releases = mock(ReleaseService.class);
        var source = mock(ReleaseSource.class);
        when(releases.history(3, 1))
                .thenReturn(List.of(new DeploymentChange("default", 4, HASH, "b".repeat(64))));
        var arguments =
                ReleaseAdministrationApplication.parse(
                        new String[] {"history", "--after-revision", "3", "--limit", "1"});
        var first = ReleaseAdministrationApplication.dispatch(arguments, releases, source);
        assertThat(PayloadJson.object(first.json()))
                .containsOnlyKeys("result", "items", "nextAfterRevision")
                .containsEntry("nextAfterRevision", 4);

        when(releases.history(3, 1)).thenReturn(List.of());
        var empty = ReleaseAdministrationApplication.dispatch(arguments, releases, source);
        assertThat(PayloadJson.object(empty.json())).containsEntry("nextAfterRevision", 3);
        assertThat(ReleaseAdministrationApplication.parse(new String[] {"history"}).limit())
                .isEqualTo(20);
        verifyNoInteractions(source);
    }

    @Test
    void onlyExplicitBusinessErrorsCanReachOutput() {
        var conflict =
                ReleaseAdministrationApplication.failure(
                        new Problem(409, "release_revision_changed"));
        assertThat(conflict.status()).isEqualTo(2);
        assertThat(PayloadJson.object(conflict.json()))
                .isEqualTo(Map.of("result", "blocked", "error", "release_revision_changed"));
        for (RuntimeException failure :
                List.of(
                        new IllegalStateException("jdbc-password-private-sentinel"),
                        new Problem(500, "manifest-private-sentinel"),
                        new Blocked("credential-private-sentinel"))) {
            assertThat(PayloadJson.object(ReleaseAdministrationApplication.failure(failure).json()))
                    .isEqualTo(Map.of("result", "blocked", "error", "release_admin_failed"));
        }
    }
}
