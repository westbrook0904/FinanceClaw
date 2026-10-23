package com.financeclawrelease;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.bootstrap.MapperConfiguration;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.infrastructure.release.EnvironmentReleaseSource;
import com.financeclaw.infrastructure.release.MyBatisReleaseRepository;
import com.financeclaw.support.PayloadJson;
import java.io.OutputStream;
import java.io.PrintStream;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.Import;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 一次性发布运维入口：登记环境候选、按预期 revision 激活或回滚，以及分页读取变更回执。
 *
 * <p>仅装配发布目录，不启动 HTTP、Worker 或 Flyway。连接和候选配置从环境/configtree 读取； 命令行拒绝任意 Spring 参数，输出不含
 * manifest、凭据或原始异常。
 */
@SpringBootConfiguration
@EnableAutoConfiguration
@Import({
    MapperConfiguration.Releases.class,
    MyBatisReleaseRepository.class,
    EnvironmentReleaseSource.class
})
public class ReleaseAdministrationApplication {
    private static final Set<String> SAFE_ERRORS =
            Set.of(
                    "release_unavailable",
                    "release_revision_changed",
                    "invalid_release_revision",
                    "invalid_release_history_page");

    public static void main(String[] args) {
        int status = run(args);
        if (status != 0) {
            System.exit(status);
        }
    }

    static int run(String[] args) {
        Arguments arguments;
        try {
            arguments = parse(args);
        } catch (IllegalArgumentException invalid) {
            System.out.println(failure("invalid_release_admin_arguments").json());
            return 2;
        }
        var output = System.out;
        var errors = System.err;
        Outcome outcome;
        // 独立 JVM 中连初始化/关闭日志也抑制，避免 DEBUG 配置在 Boot 完成前打印数据库凭据。
        try (var quiet = new PrintStream(OutputStream.nullOutputStream())) {
            System.setOut(quiet);
            System.setErr(quiet);
            try {
                outcome = execute(arguments);
            } catch (RuntimeException failed) {
                outcome = failure(failed);
            } finally {
                System.setOut(output);
                System.setErr(errors);
            }
        }
        output.println(outcome.json());
        return outcome.status();
    }

    private static Outcome execute(Arguments arguments) {
        var application = new SpringApplication(ReleaseAdministrationApplication.class);
        application.setLogStartupInfo(false);
        // 只传入固定框架参数，用户参数已解析，不参与 Spring Environment 覆盖。
        try (var context =
                application.run(
                        "--spring.main.web-application-type=none",
                        "--spring.flyway.enabled=false",
                        "--spring.sql.init.mode=never",
                        "--spring.main.banner-mode=off",
                        "--logging.config=classpath:privacy-recovery-logback.xml",
                        "--logging.level.root=OFF",
                        "--management.endpoint.health.group.readiness.include=db",
                        "--financeclaw.worker.enabled=false",
                        "--financeclaw.integrations.enabled=false",
                        "--financeclaw.feishu.enabled=false")) {
            var source = context.getBean(ReleaseSource.class);
            var repository = context.getBean(ReleaseRepository.class);
            var transactions = context.getBean(TransactionTemplate.class);
            // 查看/回滚已有发布不解析环境候选；坏配置不能阻断运维恢复通道。
            var releases =
                    arguments.command().equals("prepare")
                            ? new ReleaseService(source, repository, transactions)
                            : ReleaseService.open(repository, transactions);
            return dispatch(arguments, releases, source);
        }
    }

    static Outcome dispatch(Arguments arguments, ReleaseService releases, ReleaseSource source) {
        Object result =
                switch (arguments.command()) {
                    case "prepare" ->
                            Map.of(
                                    "result", "prepared",
                                    "candidateHash", releases.register(source.initial()),
                                    "current", releases.current());
                    case "activate" ->
                            Map.of(
                                    "result",
                                    "activated",
                                    "current",
                                    releases.activate(
                                            arguments.hash(), arguments.expectedRevision()));
                    case "history" -> {
                        var changes =
                                releases.history(arguments.afterRevision(), arguments.limit());
                        long next =
                                changes.isEmpty()
                                        ? arguments.afterRevision()
                                        : changes.getLast().revision();
                        yield Map.of(
                                "result", "history",
                                "items", changes,
                                "nextAfterRevision", next);
                    }
                    default ->
                            throw new IllegalArgumentException("invalid_release_admin_arguments");
                };
        return new Outcome(0, PayloadJson.canonical(result));
    }

    static Arguments parse(String[] args) {
        if (args == null || args.length == 0) {
            throw new IllegalArgumentException("invalid_release_admin_arguments");
        }
        String command = args[0];
        Set<String> allowed =
                switch (command) {
                    case "prepare" -> Set.of();
                    case "activate" -> Set.of("--hash", "--expected-revision");
                    case "history" -> Set.of("--after-revision", "--limit");
                    default ->
                            throw new IllegalArgumentException("invalid_release_admin_arguments");
                };
        if (args.length % 2 != 1) {
            throw new IllegalArgumentException("invalid_release_admin_arguments");
        }
        var options = new HashMap<String, String>();
        for (int index = 1; index < args.length; index += 2) {
            if (!allowed.contains(args[index])
                    || options.putIfAbsent(args[index], args[index + 1]) != null) {
                throw new IllegalArgumentException("invalid_release_admin_arguments");
            }
        }
        String hash = options.get("--hash");
        long expectedRevision = 0;
        if (command.equals("activate")) {
            if (!options.keySet().equals(allowed)
                    || hash == null
                    || !hash.matches("[0-9a-f]{64}")) {
                throw new IllegalArgumentException("invalid_release_admin_arguments");
            }
            expectedRevision = decimal(options.get("--expected-revision"), 1, Long.MAX_VALUE);
        }
        long afterRevision =
                decimal(options.getOrDefault("--after-revision", "0"), 0, Long.MAX_VALUE);
        int limit = (int) decimal(options.getOrDefault("--limit", "20"), 1, 100);
        return new Arguments(command, hash, expectedRevision, afterRevision, limit);
    }

    private static long decimal(String value, long minimum, long maximum) {
        if (value == null || !value.matches("[0-9]+")) {
            throw new IllegalArgumentException("invalid_release_admin_arguments");
        }
        long parsed = Long.parseLong(value);
        if (parsed < minimum || parsed > maximum) {
            throw new IllegalArgumentException("invalid_release_admin_arguments");
        }
        return parsed;
    }

    static Outcome failure(RuntimeException failed) {
        if ((failed instanceof Blocked || failed instanceof Problem)
                && failed.getMessage() != null
                && SAFE_ERRORS.contains(failed.getMessage())) {
            return failure(failed.getMessage());
        }
        return failure("release_admin_failed");
    }

    private static Outcome failure(String code) {
        return new Outcome(2, PayloadJson.canonical(Map.of("result", "blocked", "error", code)));
    }

    record Arguments(
            String command, String hash, long expectedRevision, long afterRevision, int limit) {}

    record Outcome(int status, String json) {}
}
