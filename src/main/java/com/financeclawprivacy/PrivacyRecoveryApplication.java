package com.financeclawprivacy;

import com.financeclaw.app.memory.PrivacyRecoveryService;
import com.financeclaw.app.memory.port.ForgetLedger;
import com.financeclaw.infrastructure.memory.*;
import com.financeclaw.support.PayloadJson;
import java.io.OutputStream;
import java.io.PrintStream;
import java.util.*;
import javax.sql.DataSource;
import org.springframework.boot.*;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.Import;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 隔离的隐私恢复入口，只初始化独立遗忘账本或将其事实重放到恢复库。
 *
 * <p>显式隔离参数是运行前置条件；启动时强制关闭 HTTP、Flyway 和模型执行。 框架输出被抑制，终端只输出不含正文或凭据的结构化结果，便于运维脚本判断恢复是否完成。
 */
@SpringBootConfiguration
@EnableAutoConfiguration
@Import({
    com.financeclaw.bootstrap.MapperConfiguration.Memory.class,
    MyBatisMemoryRepository.class,
    ForgetLedgerConfiguration.class,
    ProfileDocumentsConfiguration.class
})
public class PrivacyRecoveryApplication {
    public static void main(String[] args) {
        int status = run(args);
        if (status != 0) {
            System.exit(status);
        }
    }

    static int run(String[] args) {
        var arguments = new ArrayList<>(List.of(args));
        if (!arguments.contains("--financeclaw.privacy-recovery.isolated=true")) {
            System.out.println("{\"result\":\"blocked\",\"error\":\"isolation_required\"}");
            return 2;
        }
        var allowed =
                Set.of(
                        "--financeclaw.privacy-recovery.isolated=true",
                        "--financeclaw.privacy-recovery.action=bootstrap",
                        "--financeclaw.privacy-recovery.action=replay");
        if (arguments.size() != 2
                || new HashSet<>(arguments).size() != 2
                || !allowed.containsAll(arguments)) {
            System.out.println("{\"result\":\"blocked\",\"error\":\"invalid_recovery_arguments\"}");
            return 2;
        }
        // Forced final arguments prevent accidental web/Flyway/model startup through config files.
        arguments.addAll(
                List.of(
                        "--spring.main.web-application-type=none",
                        "--spring.flyway.enabled=false",
                        "--spring.main.banner-mode=off",
                        "--logging.config=classpath:privacy-recovery-logback.xml",
                        "--logging.level.root=OFF",
                        "--management.endpoint.health.group.readiness.include=db"));
        var output = System.out;
        var errors = System.err;
        Outcome outcome;
        // This is a dedicated, one-shot JVM. Suppress framework initialization/shutdown diagnostics
        // too: a package-specific DEBUG setting may run before Boot applies the no-appender config.
        try (var quiet = new PrintStream(OutputStream.nullOutputStream())) {
            System.setOut(quiet);
            System.setErr(quiet);
            try {
                outcome = execute(arguments);
            } catch (RuntimeException failure) {
                outcome =
                        new Outcome(
                                2,
                                "{\"result\":\"blocked\",\"error\":\"privacy_recovery_failed\"}");
            } finally {
                System.setOut(output);
                System.setErr(errors);
            }
        }
        output.println(outcome.json());
        return outcome.status();
    }

    private static Outcome execute(List<String> arguments) {
        var application = new SpringApplication(PrivacyRecoveryApplication.class);
        application.setLogStartupInfo(false);
        try (var context = application.run(arguments.toArray(String[]::new))) {
            var service =
                    new PrivacyRecoveryService(
                            context.getBean(MyBatisMemoryRepository.class),
                            new JdbcForgetRecoveryRepository(context.getBean(DataSource.class)),
                            context.getBean(ForgetLedger.class),
                            context.getBean(TransactionTemplate.class),
                            context.getBean(
                                    com.financeclaw.app.memory.port.ProfileDocuments.class));
            String action =
                    context.getEnvironment()
                            .getRequiredProperty("financeclaw.privacy-recovery.action");
            var report =
                    switch (action) {
                        case "bootstrap" -> service.bootstrap();
                        case "replay" -> service.replay();
                        default -> throw new IllegalArgumentException("invalid_recovery_action");
                    };
            return new Outcome(0, PayloadJson.canonical(report));
        }
    }

    private record Outcome(int status, String json) {}
}
