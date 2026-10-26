package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.HeartbeatGateway;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Fenced;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 专用连接与专用短事务执行原续租语句。其 mapper、事务管理器与 session 都是私有对象， 不注册为全局候选；领取、普通 fenced 写入和恢复仍使用原业务事务。
 *
 * <p>计时只使用固定结果标签，不记录 owner、attempt、SQL、参数或异常正文。连接池本身的 Hikari 指标覆盖获取/占用耗时、等待者及超时；此处计时覆盖完整事务及提交。
 */
public final class MyBatisHeartbeatGateway implements HeartbeatGateway {
    private final HeartbeatMapper mapper;
    private final TransactionTemplate tx;
    private final Map<String, Timer> timers;

    public MyBatisHeartbeatGateway(
            HeartbeatResources resources,
            int statementTimeoutSeconds,
            int transactionTimeoutSeconds,
            MeterRegistry metrics)
            throws Exception {
        var source = resources.dataSource();
        var config = new Configuration();
        config.setCacheEnabled(false);
        config.setLocalCacheScope(LocalCacheScope.STATEMENT);
        config.setDefaultStatementTimeout(statementTimeoutSeconds);
        config.addMapper(HeartbeatMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(config);
        mapper = new SqlSessionTemplate(factory.getObject()).getMapper(HeartbeatMapper.class);
        tx = new TransactionTemplate(new JdbcTransactionManager(source));
        tx.setTimeout(transactionTimeoutSeconds);
        timers =
                metrics == null
                        ? Map.of()
                        : Map.of(
                                "renewed", timer(metrics, "renewed"),
                                "cancelling", timer(metrics, "cancelling"),
                                "fenced", timer(metrics, "fenced"),
                                "error", timer(metrics, "error"));
    }

    private static Timer timer(MeterRegistry metrics, String outcome) {
        return Timer.builder("financeclaw.execution.heartbeat")
                .description("Heartbeat transaction including connection acquisition and commit")
                .tag("outcome", outcome)
                .register(metrics);
    }

    @Override
    public boolean renew(Claim claim, long leaseMillis) {
        if (leaseMillis <= 0) throw new IllegalArgumentException("Invalid lease duration");
        long started = System.nanoTime();
        String outcome = "error";
        try {
            boolean cancelling =
                    tx.execute(
                            status -> {
                                Boolean result =
                                        mapper.updateCommandLeaseAndGetCancellation(
                                                claim, leaseMillis);
                                // 必须在 callback 内抛出，保持原未命中租约的 rollback，而不是提交空事务后抛出。
                                if (result == null) throw new Fenced();
                                return result;
                            });
            outcome = cancelling ? "cancelling" : "renewed";
            return cancelling;
        } catch (Fenced fenced) {
            outcome = "fenced";
            throw fenced;
        } finally {
            Timer timer = timers.get(outcome);
            if (timer != null) timer.record(System.nanoTime() - started, TimeUnit.NANOSECONDS);
        }
    }
}
