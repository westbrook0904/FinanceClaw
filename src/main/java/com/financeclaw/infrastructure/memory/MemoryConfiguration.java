package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.*;
import com.financeclaw.app.memory.port.*;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.memory.MemoryPolicy;
import javax.sql.DataSource;
import org.springframework.context.annotation.*;
import org.springframework.core.env.Environment;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 装配画像、证据与遗忘用例，并从环境生成两层记忆的功能策略。 主库恢复仓库使用业务 DataSource 和同一
 * TransactionTemplate，独立账本使用单独连接；事件中心参与提交后的失效通知。此配置不启动提取轮询，受限 Worker 有独立入口。
 */
@Configuration(proxyBeanMethods = false)
@Import({ForgetLedgerConfiguration.class, TaskEventHub.class})
public class MemoryConfiguration {
    @Bean
    ForgetRecoveryRepository forgetRecoveryRepository(DataSource source) {
        return new JdbcForgetRecoveryRepository(source);
    }

    @Bean
    MemoryForgetService memoryForgetService(
            MemoryRepository memory,
            TransactionTemplate tx,
            ForgetRecoveryRepository recovery,
            ForgetLedger ledger,
            TaskEventHub events) {
        return new MemoryForgetService(memory, tx, recovery, ledger, events);
    }

    @Bean
    MemoryService memoryService(
            MemoryRepository memory,
            TransactionTemplate tx,
            MemoryPolicy policy,
            MemoryForgetService forgetting) {
        return new MemoryService(memory, tx, policy, forgetting);
    }

    @Bean
    UserProfileService userProfileService(
            MemoryRepository memory,
            TransactionTemplate tx,
            MemoryPolicy policy,
            TaskEventHub events) {
        return new UserProfileService(memory, tx, policy, events);
    }

    @Bean
    MemoryPolicy memoryPolicy(Environment env) {
        return new MemoryPolicy(
                env.getProperty("financeclaw.memory.enabled", Boolean.class, false),
                env.getProperty("financeclaw.memory.vector-search", Boolean.class, false),
                env.getProperty("financeclaw.memory.embedding.revision", "disabled"),
                env.getProperty("financeclaw.memory.profile-enabled", Boolean.class, true));
    }
}
