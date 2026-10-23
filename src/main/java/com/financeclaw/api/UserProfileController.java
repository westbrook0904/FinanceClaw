package com.financeclaw.api;

import com.financeclaw.app.memory.UserProfileService;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.memory.UserProfile;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.List;
import java.util.regex.Pattern;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

/**
 * 认证主体专属的 Markdown 画像接口。
 *
 * <p>正文以严格 UTF-8 读取并按字节限额；If-Match 同时绑定主体、隐私 epoch 和画像版本， 防止并发编辑覆盖或将遗忘前的文件写回。客户端不能指定服务器文件路径。
 */
@RestController
@RequestMapping("/v1/memory/profile.md")
public class UserProfileController {
    private static final Pattern VERSION =
            Pattern.compile("\"profile-([1-9][0-9]*)-([0-9]+)-([a-f0-9]{64})\"");
    private final UserProfileService profiles;

    public UserProfileController(UserProfileService profiles) {
        this.profiles = profiles;
    }

    @GetMapping
    public ResponseEntity<byte[]> read(HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        Owner owner = TaskController.owner(request);
        return document(owner, profiles.read(owner));
    }

    @PutMapping(consumes = "text/markdown")
    public ResponseEntity<byte[]> update(HttpServletRequest request, HttpServletResponse response)
            throws IOException {
        response.setHeader("Cache-Control", "no-store");
        Owner owner = TaskController.owner(request);
        // 1. 先验证主体绑定的版本前置条件，禁止无条件覆盖和跨主体导入。
        String match = request.getHeader("If-Match");
        if (match == null) {
            throw new Problem(428, "profile_precondition_required");
        }
        var version = VERSION.matcher(match);
        if (Collections.list(request.getHeaders("If-Match")).size() != 1
                || !version.matches()
                || !version.group(3).equals(ownerHash(owner))) {
            throw new Problem(422, "invalid_profile_version");
        }
        long epoch;
        long revision;
        try {
            epoch = Long.parseLong(version.group(1));
            revision = Long.parseLong(version.group(2));
        } catch (NumberFormatException invalid) {
            throw new Problem(422, "invalid_profile_version");
        }
        // 2. 正文只接受 UTF-8，并按实际读取字节限额，不信任 Content-Length。
        try {
            var charset = MediaType.parseMediaType(request.getContentType()).getCharset();
            if (charset != null && !charset.equals(StandardCharsets.UTF_8)) {
                throw new Problem(422, "invalid_profile_markdown");
            }
        } catch (IllegalArgumentException invalid) {
            throw new Problem(422, "invalid_profile_markdown");
        }
        if (request.getContentLengthLong() > UserProfile.MAX_BYTES) {
            throw new Problem(413, "profile_too_large");
        }
        byte[] bytes = request.getInputStream().readNBytes(UserProfile.MAX_BYTES + 1);
        if (bytes.length > UserProfile.MAX_BYTES) {
            throw new Problem(413, "profile_too_large");
        }
        String markdown;
        try {
            markdown =
                    StandardCharsets.UTF_8
                            .newDecoder()
                            .onMalformedInput(CodingErrorAction.REPORT)
                            .onUnmappableCharacter(CodingErrorAction.REPORT)
                            .decode(ByteBuffer.wrap(bytes))
                            .toString();
        } catch (CharacterCodingException invalid) {
            throw new Problem(422, "invalid_profile_markdown");
        }
        // 3. 应用服务在 owner 独占锁和版本 CAS 下保存，返回新 ETag 供下一次编辑使用。
        return document(owner, profiles.update(owner, epoch, revision, markdown));
    }

    private static ResponseEntity<byte[]> document(Owner owner, UserProfile profile) {
        return ResponseEntity.ok()
                .contentType(new MediaType("text", "markdown", StandardCharsets.UTF_8))
                .header("Content-Disposition", "attachment; filename=\"profile.md\"")
                .header("Cache-Control", "no-store")
                .header("X-Content-Type-Options", "nosniff")
                .eTag(
                        "\"profile-"
                                + profile.epoch()
                                + "-"
                                + profile.revision()
                                + "-"
                                + ownerHash(owner)
                                + "\"")
                .body(profile.markdown().getBytes(StandardCharsets.UTF_8));
    }

    private static String ownerHash(Owner owner) {
        return ExecutionScope.hash(List.of("user-profile", owner.tenant(), owner.subject()));
    }
}
