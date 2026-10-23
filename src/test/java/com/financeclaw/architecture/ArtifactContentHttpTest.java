package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.financeclaw.api.*;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadDigest;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class ArtifactContentHttpTest {
    private static final Owner OWNER = new Owner("tenant", "subject");
    private ArtifactService artifacts;
    private MockMvc http;

    @BeforeEach
    void setup() {
        artifacts = mock(ArtifactService.class);
        http =
                MockMvcBuilders.standaloneSetup(new ArtifactContentController(artifacts))
                        .setControllerAdvice(new ApiErrors())
                        .addFilters(new BearerIdentity("token", OWNER.tenant(), OWNER.subject()))
                        .build();
    }

    @Test
    void downloadReturnsExactJsonAsAnUncachedAttachment() throws Exception {
        var json = "{\"output\":\"海😀\\n\"}";
        var bytes = json.getBytes(StandardCharsets.UTF_8);
        when(artifacts.download(OWNER, "id"))
                .thenReturn(new ArtifactPayload.Document("id", PayloadDigest.sha256(json), bytes));
        var response =
                http.perform(
                                get("/v1/artifacts/id/download")
                                        .header("Authorization", "Bearer token"))
                        .andExpect(status().isOk())
                        .andExpect(content().contentType("application/json;charset=UTF-8"))
                        .andExpect(
                                header().string(
                                                "Content-Disposition",
                                                "attachment; filename=\"tool-result.json\""))
                        .andExpect(header().string("Cache-Control", "no-store"))
                        .andExpect(header().string("X-Content-Type-Options", "nosniff"))
                        .andExpect(header().string("X-Artifact-SHA256", PayloadDigest.sha256(json)))
                        .andReturn()
                        .getResponse();
        assertThat(response.getContentAsByteArray()).isEqualTo(bytes);
        verify(artifacts).download(OWNER, "id");
    }

    @Test
    void rangeUsesTheAuthenticatedOwnerAndExplicitByteOffsets() throws Exception {
        when(artifacts.readRange(OWNER, "id", 4, 1024))
                .thenReturn(
                        new ArtifactPayload.Range(
                                "id", "hash", 10000, 4, 1024, 1028, false, "utf-8", "chunk"));
        http.perform(
                        get("/v1/artifacts/id/range?start=4&maxBytes=1024")
                                .header("Authorization", "Bearer token"))
                .andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.text").value("chunk"))
                .andExpect(jsonPath("$.nextStart").value(1028))
                .andExpect(jsonPath("$.totalBytes").value(10000));
        verify(artifacts).readRange(OWNER, "id", 4, 1024);
    }

    @ParameterizedTest
    @ValueSource(strings = {"range", "download"})
    void authenticationPrecedesFileAccess(String endpoint) throws Exception {
        http.perform(get("/v1/artifacts/id/" + endpoint)).andExpect(status().isUnauthorized());
        verifyNoInteractions(artifacts);
    }

    @ParameterizedTest
    @ValueSource(strings = {"range", "download"})
    void deniedAccessContainsOnlyTheStableErrorWithoutFileBody(String endpoint) throws Exception {
        when(artifacts.readRange(OWNER, "id", 0, 4096)).thenThrow(new Problem(404, "not_found"));
        when(artifacts.download(OWNER, "id")).thenThrow(new Problem(404, "not_found"));
        http.perform(get("/v1/artifacts/id/" + endpoint).header("Authorization", "Bearer token"))
                .andExpect(status().isNotFound())
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(content().json("{\"error\":\"not_found\"}"));
    }
}
