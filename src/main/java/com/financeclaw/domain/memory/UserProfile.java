package com.financeclaw.domain.memory;

import com.financeclaw.domain.Task.Problem;
import java.nio.charset.StandardCharsets;

/** Owner-scoped, manually maintained Markdown. It is data, never execution policy. */
public record UserProfile(long epoch, long revision, String markdown) {
  public static final int MAX_BYTES = 8192;

  public static void validate(long epoch, long revision, String markdown) {
    if (epoch < 1 || revision < 0) throw new Problem(422, "invalid_profile_version");
    if (markdown == null || markdown.indexOf('\0') >= 0)
      throw new Problem(422, "invalid_profile_markdown");
    for (int i = 0; i < markdown.length(); i++) {
      char current = markdown.charAt(i);
      if (Character.isHighSurrogate(current)) {
        if (++i >= markdown.length() || !Character.isLowSurrogate(markdown.charAt(i)))
          throw new Problem(422, "invalid_profile_markdown");
      } else if (Character.isLowSurrogate(current)) {
        throw new Problem(422, "invalid_profile_markdown");
      }
    }
    if (markdown.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES)
      throw new Problem(413, "profile_too_large");
  }
}
