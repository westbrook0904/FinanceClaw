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

/** An authenticated Markdown document; the caller never chooses an owner or server file path. */
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
    String match = request.getHeader("If-Match");
    if (match == null) throw new Problem(428, "profile_precondition_required");
    var version = VERSION.matcher(match);
    if (Collections.list(request.getHeaders("If-Match")).size() != 1
        || !version.matches()
        || !version.group(3).equals(ownerHash(owner)))
      throw new Problem(422, "invalid_profile_version");
    long epoch;
    long revision;
    try {
      epoch = Long.parseLong(version.group(1));
      revision = Long.parseLong(version.group(2));
    } catch (NumberFormatException invalid) {
      throw new Problem(422, "invalid_profile_version");
    }
    try {
      var charset = MediaType.parseMediaType(request.getContentType()).getCharset();
      if (charset != null && !charset.equals(StandardCharsets.UTF_8))
        throw new Problem(422, "invalid_profile_markdown");
    } catch (IllegalArgumentException invalid) {
      throw new Problem(422, "invalid_profile_markdown");
    }
    if (request.getContentLengthLong() > UserProfile.MAX_BYTES)
      throw new Problem(413, "profile_too_large");
    byte[] bytes = request.getInputStream().readNBytes(UserProfile.MAX_BYTES + 1);
    if (bytes.length > UserProfile.MAX_BYTES) throw new Problem(413, "profile_too_large");
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
