package com.financeclaw.runtime;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.infrastructure.runtime.LocalRuntimeArtifactGateway;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.jar.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class LocalRuntimeArtifactRootTest {
    @TempDir Path temporary;

    @Test
    void operatorAliasIsCanonicalizedButChildSymlinksAndRootReplacementAreRejected()
            throws Exception {
        Path root = Files.createDirectory(temporary.resolve("root"));
        Path archive = root.resolve("runtime.jar");
        try (var jar = new JarOutputStream(Files.newOutputStream(archive))) {
            jar.putNextEntry(
                    new JarEntry(
                            "com/financeclaw/infrastructure/agentscope/RuntimeArtifacts.class"));
            jar.write(new byte[] {1, 2, 3});
            jar.closeEntry();
        }
        String hash =
                HexFormat.of()
                        .formatHex(
                                MessageDigest.getInstance("SHA-256")
                                        .digest(Files.readAllBytes(archive)));
        Path alias = temporary.resolve("operator-alias");
        Files.createSymbolicLink(alias, root.toRealPath());
        var gateway = new LocalRuntimeArtifactGateway(mock(DataSource.class), alias.toString());
        assertThat(gateway.inspect("runtime.jar", hash, 1, List.of("agentscope")).artifactHash())
                .isEqualTo(hash);
        Files.createSymbolicLink(root.resolve("child.jar"), archive.toRealPath());
        assertThatThrownBy(() -> gateway.inspect("child.jar", hash, 1, List.of("agentscope")))
                .hasMessage("artifact_symlink");
        Files.move(root, temporary.resolve("moved-root"));
        Files.createDirectory(root);
        Files.copy(temporary.resolve("moved-root/runtime.jar"), root.resolve("runtime.jar"));
        assertThatThrownBy(() -> gateway.inspect("runtime.jar", hash, 1, List.of("agentscope")))
                .hasMessage("artifact_directory_changed");
        Files.delete(root.resolve("runtime.jar"));
        Files.delete(root);
        Files.createSymbolicLink(root, temporary.resolve("moved-root").toRealPath());
        assertThatThrownBy(() -> gateway.inspect("runtime.jar", hash, 1, List.of("agentscope")))
                .hasMessage("artifact_directory_changed");
    }
}
