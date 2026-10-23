package com.financeclaw.app.release;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class ReleaseServiceTest {
    private final ReleaseRepository repository = mock(ReleaseRepository.class);
    private final ReleaseDefinition initial = definition("initial");
    private final String initialHash = ReleaseIdentity.hash(initial);
    private ReleaseService releases;

    @BeforeEach
    void setup() {
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        when(repository.findManifest(initialHash)).thenReturn(initial);
        when(repository.current("default")).thenReturn(new Deployment("default", initialHash, 1));
        releases = new ReleaseService(() -> initial, repository, new TransactionTemplate(manager));
    }

    @Test
    void everyReadVerifiesManifestHashAndDoesNotSubstituteCurrentDefinition() {
        when(repository.findManifest(initialHash)).thenReturn(definition("tampered"));
        assertThatThrownBy(() -> releases.require(initialHash))
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        assertThatThrownBy(() -> releases.require("f".repeat(64)))
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        assertThatThrownBy(releases::currentHash).hasMessage("release_unavailable");
    }

    @Test
    void currentPointerIsReadFromRepositoryOnEachCall() {
        var next = definition("next");
        String hash = ReleaseIdentity.hash(next);
        when(repository.findManifest(hash)).thenReturn(next);
        when(repository.current("default")).thenReturn(new Deployment("default", hash, 2));
        assertThat(releases.currentHash()).isEqualTo(hash);
        assertThat(releases.require(initialHash)).isEqualTo(initial);
    }

    @Test
    void invalidHistoryAndRevisionDoNotReachMutationBoundary() {
        assertThatThrownBy(() -> releases.activate(initialHash, 0))
                .hasMessage("invalid_release_revision");
        assertThatThrownBy(() -> releases.history(-1, 10))
                .hasMessage("invalid_release_history_page");
        assertThatThrownBy(() -> releases.history(0, 101))
                .hasMessage("invalid_release_history_page");
        verify(repository, never()).updateDeployment(anyString(), anyString(), anyLong());
    }

    @Test
    void openingExistingCatalogDoesNotInitializeOrParseStartupConfiguration() {
        var stored = mock(ReleaseRepository.class);
        var manager = mock(PlatformTransactionManager.class);
        var opened = ReleaseService.open(stored, new TransactionTemplate(manager));
        verifyNoInteractions(stored, manager);

        var change = new DeploymentChange("default", 1, initialHash, null);
        when(stored.history("default", 0, 10)).thenReturn(List.of(change));
        assertThat(opened.history(0, 10)).containsExactly(change);
        verify(stored).history("default", 0, 10);
        verifyNoMoreInteractions(stored);
        verifyNoInteractions(manager);
    }

    @Test
    void openingMissingCatalogDoesNotInventPointerOrManifest() {
        var stored = mock(ReleaseRepository.class);
        var opened =
                ReleaseService.open(
                        stored, new TransactionTemplate(mock(PlatformTransactionManager.class)));
        assertThatThrownBy(opened::currentHash)
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        verify(stored).current("default");
        verifyNoMoreInteractions(stored);
    }

    private static ReleaseDefinition definition(String version) {
        return new ReleaseDefinition(
                version,
                "finance_agent",
                ReleaseDefinition.SCOPES,
                ReleaseDefinition.OFFLINE,
                null,
                12,
                32,
                new BusinessFeatures(List.of(), "test-skills", "{}", null));
    }
}
