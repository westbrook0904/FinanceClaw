package com.financeclaw.infrastructure.agentscope;

import static org.assertj.core.api.Assertions.*;

import java.nio.file.*;
import java.util.jar.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class RuntimeArtifactsTest {
    @TempDir Path directory;

    @Test
    void explodedAndJarFingerprintUsesSortedProductionClassesOnly() throws Exception {
        Path classes = directory.resolve("classes");
        Path a = classes.resolve("com/financeclaw/A.class");
        Path b = classes.resolve("com/financeclaw/nested/B.class");
        Files.createDirectories(b.getParent());
        Files.write(a, new byte[] {1, 2, 3});
        Files.write(b, new byte[] {4, 5});
        Path tests = directory.resolve("test-classes/com/financeclaw/Test.class");
        Files.createDirectories(tests.getParent());
        Files.write(tests, new byte[] {90});
        Files.writeString(classes.resolve("com/financeclaw/resource.txt"), "not bytecode");
        Path jar = directory.resolve("production.jar");
        try (var output = new JarOutputStream(Files.newOutputStream(jar))) {
            for (Path path : new Path[] {b, a}) {
                output.putNextEntry(
                        new JarEntry(classes.relativize(path).toString().replace('\\', '/')));
                output.write(Files.readAllBytes(path));
                output.closeEntry();
            }
        }
        String hash = RuntimeArtifacts.hashSource(classes.toUri().toString());
        assertThat(RuntimeArtifacts.hashSource("jar:" + jar.toUri() + "!/")).isEqualTo(hash);
        Path boot = directory.resolve("boot-layout.jar");
        try (var output = new JarOutputStream(Files.newOutputStream(boot))) {
            for (Path path : new Path[] {b, a}) {
                output.putNextEntry(
                        new JarEntry(
                                "BOOT-INF/classes/"
                                        + classes.relativize(path).toString().replace('\\', '/')));
                output.write(Files.readAllBytes(path));
                output.closeEntry();
            }
            // Other roots, including class-shaped test data, are never part of the production
            // digest.
            output.putNextEntry(new JarEntry("test-classes/com/financeclaw/Test.class"));
            output.write(new byte[] {99});
            output.closeEntry();
        }
        assertThat(RuntimeArtifacts.hashSource("jar:" + boot.toUri() + "!/BOOT-INF/classes/"))
                .isEqualTo(hash);
        Files.write(tests, new byte[] {99});
        Files.writeString(classes.resolve("com/financeclaw/resource.txt"), "changed");
        assertThat(RuntimeArtifacts.hashSource(classes.toUri().toString())).isEqualTo(hash);
        Files.write(a, new byte[] {1, 2, 4});
        assertThat(RuntimeArtifacts.hashSource(classes.toUri().toString())).isNotEqualTo(hash);
    }

    @Test
    void missingProductionClassesFailWithControlledErrorAndActualSourceHashIsCached() {
        assertThatThrownBy(() -> RuntimeArtifacts.hashSource(directory.toUri().toString()))
                .isInstanceOf(com.financeclaw.domain.Governance.Blocked.class)
                .hasMessage("runtime_artifact_unavailable");
        assertThat(RuntimeArtifacts.hash())
                .matches("[a-f0-9]{64}")
                .isEqualTo(RuntimeArtifacts.hash());
    }
}
