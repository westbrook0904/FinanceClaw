package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.financeclaw.api.*;
import com.financeclaw.app.memory.UserProfileService;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.memory.UserProfile;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class UserProfileHttpTest {
  private static final String PATH = "/v1/memory/profile.md";
  private static final Owner OWNER = new Owner("profile-tenant", "profile-subject");
  private UserProfileService service;
  private MockMvc http;

  @BeforeEach
  void setup() {
    service = mock(UserProfileService.class);
    http = http(OWNER);
    when(service.read(OWNER)).thenReturn(new UserProfile(1, 0, ""));
  }

  private MockMvc http(Owner owner) {
    return MockMvcBuilders.standaloneSetup(new UserProfileController(service))
        .setControllerAdvice(new ApiErrors())
        .addFilters(new BearerIdentity("profile-test-token", owner.tenant(), owner.subject()))
        .build();
  }

  private String tag() throws Exception {
    return http.perform(get(PATH).header("Authorization", "Bearer profile-test-token"))
        .andExpect(status().isOk())
        .andExpect(header().string("Cache-Control", "no-store"))
        .andExpect(header().string("X-Content-Type-Options", "nosniff"))
        .andReturn()
        .getResponse()
        .getHeader("ETag");
  }

  @Test
  void rawMarkdownRoundTripsWithoutNewlineOrUnicodeNormalization() throws Exception {
    String original = "# 用户画像\r\n\r\n- 居住地：宁波\n- 回答：简洁 😀\n";
    when(service.update(OWNER, 1, 0, original)).thenReturn(new UserProfile(1, 1, original));
    String previous = tag();
    var result =
        http.perform(
                put(PATH)
                    .header("Authorization", "Bearer profile-test-token")
                    .header("If-Match", previous)
                    .contentType("text/markdown; charset=UTF-8")
                    .content(original.getBytes(StandardCharsets.UTF_8)))
            .andExpect(status().isOk())
            .andExpect(content().contentType("text/markdown;charset=UTF-8"))
            .andExpect(
                header().string("Content-Disposition", "attachment; filename=\"profile.md\""))
            .andExpect(header().string("Cache-Control", "no-store"))
            .andReturn()
            .getResponse();
    assertThat(result.getContentAsByteArray()).isEqualTo(original.getBytes(StandardCharsets.UTF_8));
    assertThat(result.getHeader("ETag")).isNotEqualTo(previous);
    verify(service).update(OWNER, 1, 0, original);
  }

  @Test
  void authenticationPrecedesVersionAndBodyValidation() throws Exception {
    http.perform(get(PATH)).andExpect(status().isUnauthorized());
    http.perform(put(PATH).contentType("text/markdown").content("private-profile"))
        .andExpect(status().isUnauthorized());
    verifyNoInteractions(service);
  }

  @Test
  void missingVersionCannotOverwriteProfile() throws Exception {
    http.perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .contentType("text/markdown")
                .content("private-profile"))
        .andExpect(status().is(428))
        .andExpect(jsonPath("$.error").value("profile_precondition_required"));
    verifyNoInteractions(service);
  }

  @ParameterizedTest
  @ValueSource(strings = {"*", "W/\"profile-1-0-abc\"", "\"1\"", "", "private-profile"})
  void invalidVersionNeverCallsApplication(String version) throws Exception {
    http.perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .header("If-Match", version)
                .contentType("text/markdown")
                .content("private-profile"))
        .andExpect(status().isUnprocessableEntity())
        .andExpect(jsonPath("$.error").value("invalid_profile_version"));
    verifyNoInteractions(service);
  }

  @Test
  void exportedVersionCannotBeUploadedUsingAnotherOwnersCredentials() throws Exception {
    String version = tag();
    clearInvocations(service);
    http(new Owner("other-tenant", OWNER.subject()))
        .perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .header("If-Match", version)
                .contentType("text/markdown")
                .content("private-profile"))
        .andExpect(status().isUnprocessableEntity())
        .andExpect(jsonPath("$.error").value("invalid_profile_version"));
    verifyNoInteractions(service);
  }

  @Test
  void multipleVersionsAndNonUtf8CharsetAreRejected() throws Exception {
    String version = tag();
    clearInvocations(service);
    http.perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .header("If-Match", version, version)
                .contentType("text/markdown")
                .content("private-profile"))
        .andExpect(status().isUnprocessableEntity())
        .andExpect(jsonPath("$.error").value("invalid_profile_version"));
    http.perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .header("If-Match", version)
                .contentType("text/markdown; charset=ISO-8859-1")
                .content("private-profile"))
        .andExpect(status().isUnprocessableEntity())
        .andExpect(jsonPath("$.error").value("invalid_profile_markdown"));
    verifyNoInteractions(service);
  }

  @Test
  void invalidUtf8AndOversizedBodiesAreRejectedBeforeDatabaseAccess() throws Exception {
    String version = tag();
    clearInvocations(service);
    http.perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .header("If-Match", version)
                .contentType("text/markdown")
                .content(new byte[] {(byte) 0xc3, (byte) 0x28}))
        .andExpect(status().isUnprocessableEntity())
        .andExpect(jsonPath("$.error").value("invalid_profile_markdown"));
    http.perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .header("If-Match", version)
                .contentType("text/markdown")
                .content("中".repeat(3000).getBytes(StandardCharsets.UTF_8)))
        .andExpect(status().isPayloadTooLarge());
    verifyNoInteractions(service);
  }

  @Test
  void emptyBodyExplicitlyClearsDocumentWithVersionCheck() throws Exception {
    when(service.update(OWNER, 1, 0, "")).thenReturn(new UserProfile(1, 0, ""));
    http.perform(
            put(PATH)
                .header("Authorization", "Bearer profile-test-token")
                .header("If-Match", tag())
                .contentType("text/markdown"))
        .andExpect(status().isOk())
        .andExpect(content().string(""));
    verify(service).update(OWNER, 1, 0, "");
  }

  @Test
  void conflictsAndFailuresDoNotEchoMarkdown() throws Exception {
    String version = tag();
    when(service.update(OWNER, 1, 0, "private-profile"))
        .thenThrow(new Problem(409, "profile_revision_conflict"))
        .thenThrow(new IllegalStateException("private-profile"));
    for (int status : new int[] {409, 503}) {
      var result =
          http.perform(
                  put(PATH)
                      .header("Authorization", "Bearer profile-test-token")
                      .header("If-Match", version)
                      .contentType("text/markdown")
                      .content("private-profile"))
              .andExpect(status().is(status))
              .andReturn()
              .getResponse();
      assertThat(result.getContentAsString()).doesNotContain("private-profile");
    }
  }
}
