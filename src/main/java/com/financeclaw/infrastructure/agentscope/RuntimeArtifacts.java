package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.domain.Governance.Blocked;
import java.io.IOException;
import java.net.JarURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.TreeMap;
import java.util.jar.JarFile;

/** Fingerprints production classes from this class's own exploded directory or executable JAR. */
public final class RuntimeArtifacts {
    private static final String PACKAGE = "com/financeclaw/";
    private static final int MAX_CLASS_BYTES = 16 * 1024 * 1024;
    private static final long MAX_ARCHIVE_CLASS_BYTES = 128L * 1024 * 1024;
    private static final int MAX_CLASSES = 20000;

    private RuntimeArtifacts() {}

    private static volatile String cachedHash;

    public static String hash() {
        String value = cachedHash;
        if (value != null) return value;
        synchronized (RuntimeArtifacts.class) {
            if (cachedHash == null) cachedHash = fingerprint();
            return cachedHash;
        }
    }

    /** 只读取受控本地归档的类字节，不创建ClassLoader或执行归档；路径约束由调用边界负责。 */
    public static String hashArchive(Path archive) {
        try (var jar = new JarFile(archive.toFile())) {
            String prefix =
                    jar.getEntry(
                                            "BOOT-INF/classes/"
                                                    + PACKAGE
                                                    + "infrastructure/agentscope/RuntimeArtifacts.class")
                                    != null
                            ? "BOOT-INF/classes/"
                            : "";
            if (jar.getEntry(prefix + PACKAGE + "infrastructure/agentscope/RuntimeArtifacts.class")
                    == null) throw new IOException("Missing runtime anchor");
            return hashSource("jar:" + archive.toUri() + "!/" + prefix);
        } catch (Exception failure) {
            throw new Blocked("runtime_artifact_unavailable");
        }
    }

    private static String fingerprint() {
        URL anchor = RuntimeArtifacts.class.getResource("RuntimeArtifacts.class");
        if (anchor == null) throw new Blocked("runtime_artifact_unavailable");
        String location = anchor.toExternalForm();
        String suffix = PACKAGE + "infrastructure/agentscope/RuntimeArtifacts.class";
        if (!location.endsWith(suffix)) throw new Blocked("runtime_artifact_unavailable");
        return hashSource(location.substring(0, location.length() - suffix.length()));
    }

    /** The root is anchored to production code, never the whole classpath or test-classes. */
    static String hashSource(String root) {
        try {
            var entries = new TreeMap<String, byte[]>();
            if (root.startsWith("file:")) {
                Path source = Path.of(URI.create(root));
                try (var paths = Files.walk(source.resolve(PACKAGE))) {
                    for (var path :
                            paths.filter(Files::isRegularFile)
                                    .filter(path -> path.toString().endsWith(".class"))
                                    .toList()) {
                        entries.put(
                                source.relativize(path).toString().replace('\\', '/'),
                                Files.readAllBytes(path));
                    }
                }
            } else if (root.startsWith("jar:")) {
                // Open the archive root, not a directory entry. JARs need not contain explicit
                // directory entries. Boot's nested URL connection exposes its classes directory
                // as a JarFile; ordinary JAR URLs use the prefix after the final !/ instead.
                int separator = root.lastIndexOf("!/");
                if (separator < 0) throw new IOException("Invalid archive root");
                String prefix = root.substring(separator + 2);
                var connection = new URL(root.substring(0, separator + 2)).openConnection();
                connection.setUseCaches(false);
                if (!(connection instanceof JarURLConnection archive)) {
                    throw new IOException("Unsupported archive source");
                }
                try (var jar = archive.getJarFile()) {
                    long total = 0;
                    var iterator = jar.entries();
                    while (iterator.hasMoreElements()) {
                        var entry = iterator.nextElement();
                        String name = entry.getName();
                        if (entry.isDirectory()
                                || !name.startsWith(prefix + PACKAGE)
                                || !name.endsWith(".class")) continue;
                        try (var input = jar.getInputStream(entry)) {
                            byte[] bytes = input.readNBytes(MAX_CLASS_BYTES + 1);
                            total += bytes.length;
                            if (bytes.length > MAX_CLASS_BYTES
                                    || total > MAX_ARCHIVE_CLASS_BYTES
                                    || entries.size() >= MAX_CLASSES)
                                throw new IOException("Runtime archive exceeds limits");
                            if (entries.put(name.substring(prefix.length()), bytes) != null) {
                                throw new IOException("Duplicate production class");
                            }
                        }
                    }
                }
            } else {
                throw new IOException("Unsupported production source");
            }
            if (entries.isEmpty()) throw new IOException("No production classes");
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            digest.update("financeclaw-runtime-classes-v1".getBytes(StandardCharsets.UTF_8));
            for (var entry : entries.entrySet()) {
                byte[] path = entry.getKey().getBytes(StandardCharsets.UTF_8);
                digest.update(ByteBuffer.allocate(4).putInt(path.length).array());
                digest.update(path);
                digest.update(ByteBuffer.allocate(4).putInt(entry.getValue().length).array());
                digest.update(entry.getValue());
            }
            return HexFormat.of().formatHex(digest.digest());
        } catch (Exception e) {
            throw new Blocked("runtime_artifact_unavailable");
        }
    }
}
