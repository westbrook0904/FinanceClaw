package com.financeclaw.runtime;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import java.nio.file.*;
import java.util.jar.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class RuntimeArchiveSafetyTest {
    @TempDir Path directory;
    private static final String ANCHOR =
            "com/financeclaw/infrastructure/agentscope/RuntimeArtifacts.class";

    @Test
    void ignoresJarOrderingAndMetadataButRequiresRuntimeAnchor() throws Exception {
        Path first = directory.resolve("one.jar"),
                second = directory.resolve("two.jar"),
                foreign = directory.resolve("foreign.jar");
        try (var jar = new JarOutputStream(Files.newOutputStream(first))) {
            entry(jar, ANCHOR, new byte[] {1, 2, 3});
            entry(jar, "com/financeclaw/Example.class", new byte[] {4, 5});
        }
        try (var jar = new JarOutputStream(Files.newOutputStream(second))) {
            entry(jar, "META-INF/untrusted.txt", new byte[] {8, 9});
            entry(jar, "com/financeclaw/Example.class", new byte[] {4, 5});
            entry(jar, ANCHOR, new byte[] {1, 2, 3});
        }
        try (var jar = new JarOutputStream(Files.newOutputStream(foreign))) {
            entry(jar, "com/financeclaw/Example.class", new byte[] {4, 5});
        }
        assertThat(RuntimeArtifacts.hashArchive(first))
                .isEqualTo(RuntimeArtifacts.hashArchive(second));
        assertThatThrownBy(() -> RuntimeArtifacts.hashArchive(foreign))
                .hasMessage("runtime_artifact_unavailable");
    }

    @Test
    void highlyCompressedClassCannotAllocateUnboundedDecompressedMemory() throws Exception {
        Path bomb = directory.resolve("oversized.jar");
        try (var jar = new JarOutputStream(Files.newOutputStream(bomb))) {
            jar.putNextEntry(new JarEntry(ANCHOR));
            byte[] block = new byte[1024 * 1024];
            for (int i = 0; i < 17; i++) jar.write(block);
            jar.closeEntry();
        }
        assertThat(Files.size(bomb)).isLessThan(1024 * 1024);
        assertThatThrownBy(() -> RuntimeArtifacts.hashArchive(bomb))
                .hasMessage("runtime_artifact_unavailable");
    }

    private void entry(JarOutputStream jar, String name, byte[] bytes) throws Exception {
        jar.putNextEntry(new JarEntry(name));
        jar.write(bytes);
        jar.closeEntry();
    }
}
