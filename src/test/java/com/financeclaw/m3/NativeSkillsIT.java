package com.financeclaw.m3;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.agent.ModelFactory;
import com.financeclaw.application.ReleaseCatalog.ModelProfile;
import com.financeclaw.tools.BusinessCatalog;
import com.financeclaw.tools.PortfolioReview;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.model.transport.*;
import io.agentscope.core.skill.repository.ClasspathSkillRepository;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import reactor.core.publisher.Flux;

class NativeSkillsIT {
  @TempDir Path temp;

  @Test
  void packagedBootJarLoadsSkillsThroughNativeRepository() throws Exception {
    Path log = temp.resolve("packaged-skills.log");
    var process =
        new ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-Dloader.main=" + Probe.class.getName(),
                "-Dloader.path=" + Path.of("target/test-classes").toAbsolutePath(),
                "-cp",
                Path.of("target/financeclaw-java-0.1.0-SNAPSHOT.jar").toAbsolutePath().toString(),
                "org.springframework.boot.loader.launch.PropertiesLauncher")
            .redirectErrorStream(true)
            .redirectOutput(log.toFile())
            .start();
    if (!process.waitFor(20, TimeUnit.SECONDS)) {
      process.destroyForcibly();
      fail("Packaged Skill probe timed out");
    }
    assertThat(process.exitValue()).withFailMessage(Files.readString(log)).isZero();
    assertThat(Files.readString(log))
        .contains("PACKAGED_SKILL_HASH=" + BusinessCatalog.skillHash());
  }

  public static class Probe {
    public static void main(String[] args) throws Exception {
      // This process loads production classes/resources from the executable Boot jar.
      try (var repo = new ClasspathSkillRepository(BusinessCatalog.SKILLS)) {
        var skill = repo.getSkill("cocktail-from-what-i-have");
        if (skill == null || !skill.getResources().get("LICENSE").contains("MIT"))
          throw new IllegalStateException("Packaged skill missing");
        System.out.println("PACKAGED_SKILL_HASH=" + BusinessCatalog.skillHash());
      }
    }
  }

  @Test
  void nativeLoadedSkillBodyIsIncludedInFinalProviderBudgetBeforeNetwork() throws Exception {
    var networkCalls = new AtomicInteger();
    var attempts = new AtomicInteger();
    var transport =
        new HttpTransport() {
          public HttpResponse execute(HttpRequest request) {
            throw new AssertionError("No network expected");
          }

          public Flux<String> stream(HttpRequest request) {
            networkCalls.incrementAndGet();
            return Flux.error(new AssertionError("Oversized request reached transport"));
          }

          public void close() {}
        };
    try (var repo = new ClasspathSkillRepository(BusinessCatalog.SKILLS);
        var provider =
            ModelFactory.create(
                new ModelProfile("budget-test", "http://127.0.0.1/v1", 1024, 4096, 128, 32, false),
                "test-only",
                transport)) {
      String skillId = repo.getSkill("cocktail-from-what-i-have").getSkillId();
      Model scriptedFirst =
          new Model() {
            public String getModelName() {
              return "scripted-then-native-budget";
            }

            public Flux<ChatResponse> stream(
                List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
              if (attempts.getAndIncrement() == 0)
                return Flux.just(
                    ChatResponse.builder()
                        .id("load")
                        .content(
                            List.of(
                                PortfolioReview.call(
                                    "load-1",
                                    "load_skill_through_path",
                                    Map.of("skillId", skillId, "path", "SKILL.md"))))
                        .build());
              assertThat(
                      messages.stream()
                          .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                          .toList())
                  .anySatisfy(
                      r ->
                          assertThat(r.getOutput().toString())
                              .contains("## Framework: Build From The Shelf"));
              return provider.stream(messages, tools, options);
            }
          };
      try (var agent =
          ReActAgent.builder()
              .name("budget-test")
              .model(scriptedFirst)
              .skillRepository(repo)
              .skillCodeExecutionEnabled(false)
              .maxRetries(1)
              .build()) {
        assertThatThrownBy(
                () ->
                    agent.call(new UserMessage("user", "使用调酒 Skill")).block(Duration.ofSeconds(10)))
            .isInstanceOf(RuntimeException.class);
        assertThat(attempts).hasValue(2);
        assertThat(networkCalls).hasValue(0);
      }
    }
  }
}
