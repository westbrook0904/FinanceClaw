package com.financeclaw.bootstrap;

import static org.assertj.core.api.Assertions.*;

import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.autoconfigure.task.TaskExecutionAutoConfiguration;
import org.springframework.boot.env.YamlPropertySourceLoader;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.core.env.MapPropertySource;
import org.springframework.core.io.ClassPathResource;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.scheduling.annotation.ScheduledAnnotationBeanPostProcessor;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;

class SchedulingConfigurationTest {
  @Test
  void productConfigurationKeepsABoundedHttpExecutorAlongsideCustomSchedulers() throws Exception {
    var properties =
        new YamlPropertySourceLoader().load("product", new ClassPathResource("application.yml"));
    new ApplicationContextRunner()
        .withConfiguration(AutoConfigurations.of(TaskExecutionAutoConfiguration.class))
        .withUserConfiguration(
            SchedulingConfiguration.class, ExecutionSchedulingConfiguration.class)
        .withInitializer(
            context ->
                properties.forEach(
                    property -> context.getEnvironment().getPropertySources().addLast(property)))
        .run(
            context -> {
              assertThat(context).hasNotFailed();
              var http = context.getBean("applicationTaskExecutor", ThreadPoolTaskExecutor.class);
              assertThat(http.getCorePoolSize()).isEqualTo(8);
              assertThat(http.getMaxPoolSize()).isEqualTo(8);
              assertThat(http.getQueueCapacity()).isEqualTo(256);
              assertThat(context.getBeansOfType(ThreadPoolTaskScheduler.class))
                  .containsOnlyKeys("taskScheduler", "executionHeartbeatScheduler");
            });
  }

  @Test
  void qualifiedHeartbeatRunsWhileEveryDefaultSchedulerThreadIsOccupied() throws Exception {
    var release = new CountDownLatch(1);
    try (var context = context(true)) {
      var background = context.getBean("taskScheduler", ThreadPoolTaskScheduler.class);
      var heartbeat = context.getBean("executionHeartbeatScheduler", ThreadPoolTaskScheduler.class);
      var probe = context.getBean(ExecutionProbe.class);
      assertThat(background).isNotSameAs(heartbeat);
      assertThat(background.getScheduledThreadPoolExecutor().getCorePoolSize()).isEqualTo(2);
      assertThat(heartbeat.getScheduledThreadPoolExecutor().getCorePoolSize()).isEqualTo(1);
      var occupied = new CountDownLatch(2);
      for (int i = 0; i < 2; i++)
        background.execute(
            () -> {
              occupied.countDown();
              try {
                release.await(10, TimeUnit.SECONDS);
              } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
              }
            });
      assertThat(occupied.await(5, TimeUnit.SECONDS)).isTrue();

      probe.armed.set(true);
      assertThat(probe.heartbeat.await(2, TimeUnit.SECONDS)).isTrue();
      assertThat(probe.heartbeatThread.get()).startsWith("execution-heartbeat-");
      assertThat(probe.background.getCount()).isEqualTo(1);

      release.countDown();
      assertThat(probe.background.await(2, TimeUnit.SECONDS)).isTrue();
      assertThat(probe.backgroundThread.get()).startsWith("background-");
    } finally {
      release.countDown();
    }
  }

  @Test
  void defaultConfigurationHasNoExecutionSchedulerAndResolvesUnqualifiedScheduledTasks()
      throws Exception {
    try (var context = context(false)) {
      assertThat(context.getBeansOfType(ThreadPoolTaskScheduler.class))
          .containsOnlyKeys("taskScheduler");
      var probe = context.getBean(DefaultProbe.class);
      assertThat(probe.ran.await(2, TimeUnit.SECONDS)).isTrue();
      assertThat(probe.thread.get()).startsWith("background-");
    }
  }

  private static AnnotationConfigApplicationContext context(boolean execute) {
    var context = new AnnotationConfigApplicationContext();
    context
        .getEnvironment()
        .getPropertySources()
        .addFirst(new MapPropertySource("test", Map.of("spring.task.scheduling.pool.size", 2)));
    context.register(SchedulingConfiguration.class);
    // Register only annotation scheduling infrastructure; no Boot, database or application beans.
    context.registerBean(ScheduledAnnotationBeanPostProcessor.class);
    if (execute) {
      context.register(ExecutionSchedulingConfiguration.class);
      context.registerBean(ExecutionProbe.class);
    } else {
      context.registerBean(DefaultProbe.class);
    }
    context.refresh();
    return context;
  }

  static class ExecutionProbe {
    final AtomicBoolean armed = new AtomicBoolean();
    final CountDownLatch heartbeat = new CountDownLatch(1);
    final CountDownLatch background = new CountDownLatch(1);
    final AtomicReference<String> heartbeatThread = new AtomicReference<>();
    final AtomicReference<String> backgroundThread = new AtomicReference<>();

    @Scheduled(fixedDelay = 10, scheduler = "executionHeartbeatScheduler")
    public void heartbeat() {
      if (armed.get()) {
        heartbeatThread.set(Thread.currentThread().getName());
        heartbeat.countDown();
      }
    }

    @Scheduled(fixedDelay = 10)
    public void background() {
      if (armed.get()) {
        backgroundThread.set(Thread.currentThread().getName());
        background.countDown();
      }
    }
  }

  static class DefaultProbe {
    final CountDownLatch ran = new CountDownLatch(1);
    final AtomicReference<String> thread = new AtomicReference<>();

    @Scheduled(fixedDelay = 10)
    public void tick() {
      thread.set(Thread.currentThread().getName());
      ran.countDown();
    }
  }
}
