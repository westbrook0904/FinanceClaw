package com.financeclaw.fixture;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.app.release.port.StoredRelease;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.HashMap;
import java.util.Map;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

/** Unit-test fixture only; durable behavior is verified separately against PostgreSQL. */
public final class TestReleases {
    private TestReleases() {}

    public static ReleaseService create(ReleaseSource source) {
        Map<String, ReleaseDefinition> manifests = new HashMap<>();
        Map<String, Deployment> pointers = new HashMap<>();
        var repository = mock(ReleaseRepository.class);
        doAnswer(
                        call -> {
                            manifests.putIfAbsent(call.getArgument(0), call.getArgument(1));
                            return null;
                        })
                .when(repository)
                .insertManifestIfAbsent(anyString(), any());
        when(repository.findManifest(anyString()))
                .thenAnswer(call -> manifests.get(call.getArgument(0)));
        when(repository.findStoredRelease(anyString()))
                .thenAnswer(
                        call -> {
                            String hash = call.getArgument(0);
                            var definition = manifests.get(hash);
                            return definition == null
                                    ? null
                                    : new StoredRelease.Legacy(hash, definition);
                        });
        when(repository.current(anyString())).thenAnswer(call -> pointers.get(call.getArgument(0)));
        when(repository.insertDeploymentIfAbsent(anyString(), anyString()))
                .thenAnswer(
                        call -> {
                            String name = call.getArgument(0);
                            return pointers.putIfAbsent(
                                            name, new Deployment(name, call.getArgument(1), 1))
                                    == null;
                        });
        when(repository.updateDeployment(anyString(), anyString(), anyLong()))
                .thenAnswer(
                        call -> {
                            String name = call.getArgument(0);
                            long revision = call.getArgument(2);
                            var old = pointers.get(name);
                            if (old == null || old.revision() != revision) {
                                return false;
                            }
                            pointers.put(
                                    name, new Deployment(name, call.getArgument(1), revision + 1));
                            return true;
                        });
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        return new ReleaseService(source, repository, new TransactionTemplate(manager));
    }
}
