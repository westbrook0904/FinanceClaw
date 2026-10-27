package com.financeclaw.infrastructure.runtime;

import com.financeclaw.app.runtime.port.RuntimeArtifactGateway;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.runtime.RuntimeArtifact;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;

/** 文件只从操作员配置的本地目录读取；禁止绝对路径、穿越、符号链接和远程下载。 */
public final class LocalRuntimeArtifactGateway implements RuntimeArtifactGateway {
    private static final long MAX_BYTES = 512L * 1024 * 1024;
    private final JdbcTemplate sql;
    private final Path root;
    private final Object rootFileKey;
    private static final RowMapper<RuntimeArtifact> ROW =
            (row, index) ->
                    new RuntimeArtifact(
                            row.getString("artifact_hash"),
                            row.getString("runtime_hash"),
                            row.getString("relative_path"),
                            row.getLong("bytes"),
                            row.getInt("protocol_version"),
                            Arrays.asList((String[]) row.getArray("capabilities").getArray()));

    public LocalRuntimeArtifactGateway(JdbcTemplate sql, String root) {
        this.sql = java.util.Objects.requireNonNull(sql);
        if (root == null || root.isBlank()) {
            this.root = null;
            this.rootFileKey = null;
        } else {
            try {
                // 操作员配置可使用/tmp等可信别名；固定它当时指向的真实目录。
                this.root = Path.of(root).toAbsolutePath().normalize().toRealPath();
                var attributes =
                        Files.readAttributes(
                                this.root,
                                java.nio.file.attribute.BasicFileAttributes.class,
                                LinkOption.NOFOLLOW_LINKS);
                if (!attributes.isDirectory()) throw new java.io.IOException("Not a directory");
                this.rootFileKey = attributes.fileKey();
            } catch (Exception invalid) {
                throw new IllegalArgumentException("Invalid runtime artifact directory");
            }
        }
    }

    @Override
    public RuntimeArtifact inspect(
            String relative, String expected, int protocol, List<String> capabilities) {
        if (root == null) throw new Problem(409, "runtime_artifact_directory_disabled");
        try {
            if (relative == null || relative.isBlank() || relative.length() > 512)
                throw new Problem(422, "invalid_artifact_path");
            Path path = Path.of(relative);
            if (path.isAbsolute() || !path.normalize().equals(path) || !relative.endsWith(".jar"))
                throw new Problem(422, "invalid_artifact_path");
            // leading ../ survives Path.normalize(); reject every parent segment before any IO.
            for (Path segment : path) {
                if (segment.toString().equals(".."))
                    throw new Problem(422, "invalid_artifact_path");
            }
            requireRoot();
            Path candidate = root.resolve(path).normalize();
            if (!candidate.startsWith(root)) throw new Problem(422, "invalid_artifact_path");
            Path current = root;
            for (Path segment : path) {
                current = current.resolve(segment);
                if (Files.isSymbolicLink(current)) throw new Problem(422, "artifact_symlink");
            }
            if (!Files.isRegularFile(candidate, LinkOption.NOFOLLOW_LINKS))
                throw new Problem(404, "runtime_artifact_unavailable");
            long bytes = Files.size(candidate);
            if (bytes < 1 || bytes > MAX_BYTES) throw new Problem(422, "runtime_artifact_size");
            if (!sha256(candidate).equals(expected))
                throw new Problem(409, "runtime_artifact_hash_mismatch");
            String runtime = RuntimeArtifacts.hashArchive(candidate);
            // 校验类目录摘要期间文件不得变化；结果只是本次读取的证明，执行前必须再次核验。
            if (Files.size(candidate) != bytes || !sha256(candidate).equals(expected))
                throw new Problem(409, "runtime_artifact_changed");
            requireRoot();
            return new RuntimeArtifact(
                    expected, runtime, path.toString(), bytes, protocol, capabilities);
        } catch (Problem failure) {
            throw failure;
        } catch (Exception failure) {
            throw new Problem(409, "runtime_artifact_unavailable");
        }
    }

    private void requireRoot() throws java.io.IOException {
        var attributes =
                Files.readAttributes(
                        root,
                        java.nio.file.attribute.BasicFileAttributes.class,
                        LinkOption.NOFOLLOW_LINKS);
        if (!root.toRealPath().equals(root)
                || !attributes.isDirectory()
                || !java.util.Objects.equals(rootFileKey, attributes.fileKey()))
            throw new Problem(409, "artifact_directory_changed");
    }

    private static String sha256(Path file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = Files.newInputStream(file, LinkOption.NOFOLLOW_LINKS)) {
            byte[] buffer = new byte[65536];
            long read = 0;
            for (int n; (n = input.read(buffer)) >= 0; ) {
                read += n;
                if (read > MAX_BYTES) throw new Problem(422, "runtime_artifact_size");
                digest.update(buffer, 0, n);
            }
        }
        return HexFormat.of().formatHex(digest.digest());
    }

    @Override
    public void insert(RuntimeArtifact artifact) {
        sql.update(
                "INSERT INTO business.runtime_artifacts(artifact_hash,runtime_hash,relative_path,bytes,protocol_version,capabilities) VALUES(?,?,?,?,?,ARRAY['agentscope']) ON CONFLICT(artifact_hash) DO NOTHING",
                artifact.artifactHash(),
                artifact.runtimeHash(),
                artifact.relativePath(),
                artifact.bytes(),
                artifact.protocolVersion());
    }

    @Override
    public RuntimeArtifact get(String hash) {
        return sql
                .query("SELECT * FROM business.runtime_artifacts WHERE artifact_hash=?", ROW, hash)
                .stream()
                .findFirst()
                .orElse(null);
    }

    @Override
    public List<RuntimeArtifact> list(int limit) {
        return sql.query(
                "SELECT * FROM business.runtime_artifacts ORDER BY created_at DESC,artifact_hash LIMIT ?",
                ROW,
                limit);
    }
}
