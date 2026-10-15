package com.financeclaw.m4;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.memory.*;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;

class NativeMemoryTest {
  @Test
  void nativeStructuredExtractionHasNoBusinessToolsOrHistory() {
    try (var remote = new MockMemoryModels()) {
      var env =
          new MockEnvironment()
              .withProperty("financeclaw.memory.extractor.model", "memory-test")
              .withProperty("financeclaw.memory.extractor.base-url", remote.url())
              .withProperty("financeclaw.memory.extractor.api-key", "mock-key");
      var models = new MemoryModels(env, mock(MemoryMapper.class));
      var result = models.extract("我住在上海");
      assertThat(result.proposals()).hasSize(1);
      assertThat(result.proposals().getFirst().value()).isEqualTo("上海");
      String request = remote.requests.getFirst().toString();
      assertThat(request)
          .contains("我住在上海")
          .doesNotContain("create_report", "market_research", "load_skill", "mcp");
      assertThat(remote.requests.getFirst().path("messages").size()).isLessThanOrEqualTo(3);
    }
  }

  @Test
  void nativeEmbeddingUses1024AndProfileCannotChangeWithoutRevision() {
    try (var remote = new MockMemoryModels()) {
      var env =
          new MockEnvironment()
              .withProperty("financeclaw.memory.embedding.model", "embedding-test")
              .withProperty("financeclaw.memory.embedding.base-url", remote.url())
              .withProperty("financeclaw.memory.embedding.api-key", "mock-key")
              .withProperty("financeclaw.memory.embedding.revision", "embedding-v1");
      var db = mock(MemoryMapper.class);
      var fingerprint = new AtomicReference<String>();
      doAnswer(
              i -> {
                fingerprint.compareAndSet(null, i.getArgument(1));
                return null;
              })
          .when(db)
          .registerProfile(anyString(), anyString(), anyString());
      when(db.profile(anyString())).thenAnswer(i -> fingerprint.get());
      var models = new MemoryModels(env, db);
      var vector =
          models
              .embedding()
              .embed(
                  io.agentscope.core.message.TextBlock.builder()
                      .text("response_language: 中文")
                      .build())
              .block(java.time.Duration.ofSeconds(10));
      assertThat(vector).hasSize(1024);
      assertThat(remote.requests.getFirst().path("dimensions").asInt()).isEqualTo(1024);
      assertThat(MemoryProcessor.vector(vector)).startsWith("{1.0,");
      env.setProperty("financeclaw.memory.embedding.model", "changed");
      assertThatThrownBy(models::embedding).hasMessageContaining("new revision");
    }
  }

  @Test
  void invalidEmbeddingAndImplicitAutoConfirmationAreRejected() {
    assertThat(MemoryProcessor.explicit("/remember response_style=简洁")).isNotNull();
    assertThat(MemoryProcessor.explicit("我希望简洁一点")).isNull();
    assertThat(MemoryProcessor.explicit("/remember home_city=上海")).isNull();
    assertThatThrownBy(() -> MemoryProcessor.vector(new double[1023]))
        .hasMessage("embedding_dimension_mismatch");
    assertThatThrownBy(() -> MemoryProcessor.vector(new double[1024])).hasMessage("zero_embedding");
    var nan = new double[1024];
    nan[0] = Double.NaN;
    assertThatThrownBy(() -> MemoryProcessor.vector(nan)).hasMessage("invalid_embedding");
  }
}
