package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.ProfileDocuments;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.memory.UserProfile;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.Channels;
import java.nio.channels.FileChannel;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.List;
import java.util.Objects;
import java.util.regex.Pattern;

/**
 * 从受托管服务器目录读取人工画像，每次读取都查看当前文件，支持编辑器原子替换保存。
 *
 * <p>owner 只参与稳定哈希，不能提供路径；epoch 目录隔离遗忘前的旧文件。托管目录和文件均拒绝软链接， 正文按严格 UTF-8
 * 和字节上限读取，文件属性变化时拒绝本次受理，禁止静默退回旧数据库画像。
 */
public final class DirectoryProfileDocuments implements ProfileDocuments {
    private static final String FILE_NAME = "profile.md";
    private static final Pattern EPOCH_DIRECTORY = Pattern.compile("epoch-([1-9][0-9]*)");
    private final Path root;

    public DirectoryProfileDocuments(Path directory) {
        Objects.requireNonNull(directory);
        try {
            Path requested = directory.toAbsolutePath().normalize();
            Files.createDirectories(
                    requested,
                    PosixFilePermissions.asFileAttribute(
                            PosixFilePermissions.fromString("rwx------")));
            directoryAttributes(requested);
            // 配置根由运维指定，可以包含 /tmp 等操作系统别名；之后所有托管路径均以真实根为边界。
            root = requested.toRealPath();
            directoryAttributes(root);
        } catch (IOException | SecurityException | UnsupportedOperationException failure) {
            throw unavailable();
        }
    }

    @Override
    public boolean enabled() {
        return true;
    }

    @Override
    public String read(Owner owner, long epoch) {
        UserProfile.validate(epoch, 0, "");
        try {
            var rootBefore = directoryAttributes(root);
            Path ownerDirectory = ownerDirectory(owner);
            ensureDirectory(ownerDirectory);
            var ownerBefore = directoryAttributes(ownerDirectory);
            Path epochDirectory = ownerDirectory.resolve("epoch-" + epoch);
            ensureDirectory(epochDirectory);
            var epochBefore = directoryAttributes(epochDirectory);
            Path file = epochDirectory.resolve(FILE_NAME);
            ensureFile(file);
            var before = fileAttributes(file);
            if (before.size() > UserProfile.MAX_BYTES) {
                throw new Problem(413, "profile_too_large");
            }
            byte[] raw;
            try {
                raw = readBytes(file);
                var after = fileAttributes(file);
                if (!sameFile(before, after)
                        || !sameDirectory(rootBefore, directoryAttributes(root))
                        || !sameDirectory(ownerBefore, directoryAttributes(ownerDirectory))
                        || !sameDirectory(epochBefore, directoryAttributes(epochDirectory))) {
                    throw changed();
                }
            } catch (NoSuchFileException disappeared) {
                throw changed();
            } catch (Problem replacedPath) {
                if ("profile_file_unavailable".equals(replacedPath.getMessage())) {
                    throw changed();
                }
                throw replacedPath;
            }
            if (raw.length > UserProfile.MAX_BYTES) {
                throw new Problem(413, "profile_too_large");
            }
            String markdown;
            try {
                markdown =
                        StandardCharsets.UTF_8
                                .newDecoder()
                                .onMalformedInput(CodingErrorAction.REPORT)
                                .onUnmappableCharacter(CodingErrorAction.REPORT)
                                .decode(ByteBuffer.wrap(raw))
                                .toString();
            } catch (java.nio.charset.CharacterCodingException invalid) {
                throw new Problem(422, "invalid_profile_markdown");
            }
            UserProfile.validate(epoch, 0, markdown);
            return markdown;
        } catch (IOException | SecurityException | UnsupportedOperationException failure) {
            throw unavailable();
        }
    }

    /** 限额多读取一个字节以识别扩大的文件，不相信读取前的 stat 长度；句柄禁止跟随文件软链接。 */
    byte[] readBytes(Path file) throws IOException {
        try (var channel =
                        FileChannel.open(file, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS);
                var input = Channels.newInputStream(channel)) {
            return input.readNBytes(UserProfile.MAX_BYTES + 1);
        }
    }

    @Override
    public void eraseThrough(Owner owner, long epoch) {
        UserProfile.validate(epoch, 0, "");
        try {
            var rootBefore = directoryAttributes(root);
            Path ownerDirectory = ownerDirectory(owner);
            BasicFileAttributes ownerBefore;
            try {
                ownerBefore = directoryAttributes(ownerDirectory);
            } catch (NoSuchFileException absentOwner) {
                return;
            }
            try (var entries = Files.newDirectoryStream(ownerDirectory)) {
                for (Path entry : entries) {
                    var match = EPOCH_DIRECTORY.matcher(entry.getFileName().toString());
                    if (!match.matches() || !atMost(match.group(1), epoch)) {
                        continue;
                    }
                    // 默认 walkFileTree 不跟随软链接，目录内的链接仅删除链接本身，不访问其目标。
                    Files.walkFileTree(
                            entry,
                            new SimpleFileVisitor<>() {
                                @Override
                                public FileVisitResult visitFile(
                                        Path file, BasicFileAttributes attributes)
                                        throws IOException {
                                    Files.delete(file);
                                    return FileVisitResult.CONTINUE;
                                }

                                @Override
                                public FileVisitResult postVisitDirectory(
                                        Path directory, IOException failure) throws IOException {
                                    if (failure != null) {
                                        throw failure;
                                    }
                                    Files.delete(directory);
                                    return FileVisitResult.CONTINUE;
                                }
                            });
                }
            }
            if (!sameDirectory(rootBefore, directoryAttributes(root))
                    || !sameDirectory(ownerBefore, directoryAttributes(ownerDirectory))) {
                throw changed();
            }
        } catch (IOException
                | DirectoryIteratorException
                | SecurityException
                | UnsupportedOperationException failure) {
            throw unavailable();
        }
    }

    private Path ownerDirectory(Owner owner) {
        return root.resolve(ExecutionScope.hash(List.of(owner.tenant(), owner.subject())));
    }

    private static boolean atMost(String value, long epoch) {
        try {
            return Long.parseLong(value) <= epoch;
        } catch (NumberFormatException notManagedEpoch) {
            return false;
        }
    }

    private static void ensureDirectory(Path directory) throws IOException {
        try {
            Files.createDirectory(
                    directory,
                    PosixFilePermissions.asFileAttribute(
                            PosixFilePermissions.fromString("rwx------")));
        } catch (FileAlreadyExistsException existing) {
            // 后续 NOFOLLOW 属性检查区分正常目录、普通文件和软链接，不信任单独的 exists 判断。
        }
        directoryAttributes(directory);
    }

    private static void ensureFile(Path file) throws IOException {
        try {
            Files.createFile(
                    file,
                    PosixFilePermissions.asFileAttribute(
                            PosixFilePermissions.fromString("rw-------")));
        } catch (FileAlreadyExistsException existing) {
            // 不打开现有路径写入，下面的读取仅接受不跟随链接的普通文件。
        }
    }

    private static BasicFileAttributes directoryAttributes(Path directory) throws IOException {
        var attributes =
                Files.readAttributes(
                        directory, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
        if (!attributes.isDirectory() || attributes.isSymbolicLink()) {
            throw unavailable();
        }
        return attributes;
    }

    private static BasicFileAttributes fileAttributes(Path file) throws IOException {
        var attributes =
                Files.readAttributes(file, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
        if (!attributes.isRegularFile() || attributes.isSymbolicLink()) {
            throw unavailable();
        }
        return attributes;
    }

    private static boolean sameDirectory(BasicFileAttributes before, BasicFileAttributes after) {
        return Objects.equals(before.fileKey(), after.fileKey())
                && before.creationTime().equals(after.creationTime());
    }

    private static boolean sameFile(BasicFileAttributes before, BasicFileAttributes after) {
        return sameDirectory(before, after)
                && before.size() == after.size()
                && before.lastModifiedTime().equals(after.lastModifiedTime());
    }

    private static Problem unavailable() {
        return new Problem(503, "profile_file_unavailable");
    }

    private static Problem changed() {
        return new Problem(503, "profile_file_changed");
    }
}
