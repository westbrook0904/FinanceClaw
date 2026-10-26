package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.m6.ZiweiRuntimeProbe;
import com.tngtech.archunit.core.importer.ClassFileImporter;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Properties;
import java.util.stream.Collectors;
import java.util.zip.ZipFile;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** Product-only loading must not accidentally resolve dependencies from Maven's test classpath. */
class RuntimePackagingIT {
    @TempDir Path directory;

    @Test
    void productEntrypointsAndSingleClassRecoveryProbeLoadWithoutTestFixtures() throws Exception {
        // Loading does not resolve every method-body reference; also inspect bytecode dependencies.
        Path testClasses = Path.of("target/test-classes");
        try (var files = Files.walk(testClasses)) {
            var otherTestClasses =
                    files.filter(path -> path.toString().endsWith(".class"))
                            .map(path -> testClasses.relativize(path).toString().replace('/', '.'))
                            .map(name -> name.substring(0, name.length() - ".class".length()))
                            .filter(name -> !name.equals(ZiweiRuntimeProbe.class.getName()))
                            .collect(Collectors.toSet());
            var probeType =
                    new ClassFileImporter()
                            .importClasses(ZiweiRuntimeProbe.class)
                            .get(ZiweiRuntimeProbe.class);
            assertThat(probeType.getDirectDependenciesFromSelf())
                    .noneMatch(
                            dependency ->
                                    otherTestClasses.contains(
                                            dependency.getTargetClass().getName()));
        }
        Path jar = Path.of("target/financeclaw-java-0.1.0-SNAPSHOT.jar").toAbsolutePath();
        var urls = new ArrayList<URL>();
        urls.add(new URL("jar:" + jar.toUri().toURL() + "!/BOOT-INF/classes/"));
        try (var zip = new ZipFile(jar.toFile())) {
            var entries = zip.stream().toList();
            assertThat(entries.stream().map(java.util.zip.ZipEntry::getName))
                    .noneMatch(
                            name ->
                                    name.startsWith("BOOT-INF/classes/com/financeclaw/fixture/")
                                            || name.startsWith(
                                                    "BOOT-INF/classes/com/financeclaw/m6/"));
            assertThat(
                            entries.stream()
                                    .map(java.util.zip.ZipEntry::getName)
                                    .filter(
                                            name ->
                                                    name.startsWith(
                                                                    "BOOT-INF/classes/db/migration/")
                                                            && name.endsWith(".sql")))
                    .containsExactlyInAnyOrder(
                            "BOOT-INF/classes/db/migration/V1__initialize_application.sql",
                            "BOOT-INF/classes/db/migration/V2__release_catalog.sql",
                            "BOOT-INF/classes/db/migration/V3__conversation_release_binding.sql",
                            "BOOT-INF/classes/db/migration/V4__agent_definitions.sql",
                            "BOOT-INF/classes/db/migration/V5__agent_compositions.sql",
                            "BOOT-INF/classes/db/migration/V6__child_invocations.sql",
                            "BOOT-INF/classes/db/migration/V7__execution_privacy.sql",
                            "BOOT-INF/classes/db/migration/V8__private_agent_delegation.sql",
                            "BOOT-INF/classes/db/migration/V9__private_agent_market_leaf.sql",
                            "BOOT-INF/classes/db/migration/V10__outbox_pending_binding_index.sql");
            for (var entry : entries) {
                if (!entry.getName().startsWith("BOOT-INF/lib/")
                        || !entry.getName().endsWith(".jar")) continue;
                Path dependency = directory.resolve(Path.of(entry.getName()).getFileName());
                try (var input = zip.getInputStream(entry)) {
                    Files.copy(input, dependency);
                }
                urls.add(dependency.toUri().toURL());
            }
        }
        // Match ZiweiRecoveryIT's single-class probe boundary; no whole test-classes directory.
        String probePath = "com/financeclaw/m6/ZiweiRuntimeProbe.class";
        Path probeRoot = directory.resolve("probe");
        Files.createDirectories(probeRoot.resolve(probePath).getParent());
        Files.copy(Path.of("target/test-classes").resolve(probePath), probeRoot.resolve(probePath));
        urls.add(probeRoot.toUri().toURL());
        try (var loader =
                new URLClassLoader(
                        urls.toArray(URL[]::new), ClassLoader.getPlatformClassLoader())) {
            for (String name :
                    new String[] {
                        "com.financeclaw.fixture.TaskFixture",
                        "com.financeclaw.fixture.GovernanceFixture"
                    }) {
                assertThatThrownBy(() -> Class.forName(name, true, loader))
                        .isInstanceOf(ClassNotFoundException.class);
            }
            for (String name :
                    new String[] {
                        "com.financeclaw.bootstrap.FinanceClawApplication",
                        "com.financeclawsetup.DatabaseInitializationApplication",
                        "com.financeclawmemory.MemoryWorkerApplication",
                        "com.financeclawprivacy.PrivacyRecoveryApplication",
                        "com.financeclawrelease.ReleaseAdministrationApplication",
                        "com.financeclawagent.AgentAdministrationApplication",
                        "com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime",
                        "com.financeclaw.infrastructure.agentscope.execution.GovernedTools",
                        "com.financeclaw.infrastructure.agentscope.capability.McpToolContribution"
                    }) {
                var type = Class.forName(name, true, loader);
                assertThat(type.getClassLoader()).isSameAs(loader);
                assertThat(type.getDeclaredConstructors()).isNotEmpty();
                assertThat(type.getDeclaredMethods()).isNotEmpty();
            }
            var probe = Class.forName("com.financeclaw.m6.ZiweiRuntimeProbe", true, loader);
            var runtimeArtifacts =
                    Class.forName(
                            "com.financeclaw.infrastructure.agentscope.RuntimeArtifacts",
                            true,
                            loader);
            assertThat(runtimeArtifacts.getMethod("hash").invoke(null))
                    .isEqualTo(com.financeclaw.infrastructure.agentscope.RuntimeArtifacts.hash());
            assertThat(probe.getDeclaredConstructors()).hasSize(1);
            var constructor = probe.getDeclaredConstructors()[0];
            assertThat(constructor.getParameterCount()).isEqualTo(10);
            constructor.setAccessible(true);
            Object instance =
                    constructor.newInstance(
                            null, null, null, null, null, null, null, null, null, new Properties());
            assertThat(probe.getMethod("getModelName").invoke(instance))
                    .isEqualTo("m6d-scripted-test-only");
        }
    }
}
