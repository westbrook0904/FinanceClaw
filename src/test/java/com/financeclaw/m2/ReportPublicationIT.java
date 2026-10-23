package com.financeclaw.m2;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.governance.MyBatisReportGateway;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 当前发布契约：完整报告与精确回执同写、同回滚，重复调用不新增实体。 */
class ReportPublicationIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static JdbcTemplate sql;
    private static GovernanceMapper mapper;
    private static TransactionTemplate tx;

    @BeforeAll
    static void initialize() throws Exception {
        PG.start();
        var source =
                new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        Flyway.configure().dataSource(source).load().migrate();
        sql = new JdbcTemplate(source);
        tx = new TransactionTemplate(new JdbcTransactionManager(source));
        var config = new Configuration();
        config.setMapUnderscoreToCamelCase(true);
        config.setCacheEnabled(false);
        config.setLocalCacheScope(LocalCacheScope.STATEMENT);
        config.addMapper(GovernanceMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(config);
        mapper = new SqlSessionTemplate(factory.getObject()).getMapper(GovernanceMapper.class);
    }

    @AfterAll
    static void stop() {
        PG.stop();
    }

    @BeforeEach
    void newTurn() {
        sql.execute("TRUNCATE business.conversations CASCADE");
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES('conversation','tenant','subject')");
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES('turn','conversation','tenant','subject','key','request-hash','publish','release','running')");
    }

    @Test
    void oneInsertCreatesTheWholePublicationAndRollbackRemovesIt() {
        String receipt = "{\"report_id\":\"new-report\",\"status\":\"created\"}";
        tx.executeWithoutResult(
                status -> {
                    assertThat(
                                    mapper.insertReportPublication(
                                            "new-report",
                                            "turn",
                                            "call",
                                            "title",
                                            "action",
                                            receipt))
                            .isOne();
                    assertThat(mapper.getReportReceipt("turn", "call").result()).isEqualTo(receipt);
                    assertThat(
                                    sql.queryForMap(
                                            "SELECT id,title,action_hash,result FROM business.reports"))
                            .containsEntry("id", "new-report")
                            .containsEntry("title", "title")
                            .containsEntry("action_hash", "action")
                            .containsEntry("result", receipt);
                    status.setRollbackOnly();
                });
        assertThat(snapshot()).isEmpty();
        assertThat(
                        mapper.insertReportPublication(
                                "new-report", "turn", "call", "title", "action", receipt))
                .isOne();
        var published = snapshot();
        assertThatThrownBy(
                        () ->
                                mapper.insertReportPublication(
                                        "second-report",
                                        "turn",
                                        "call",
                                        "changed",
                                        "action",
                                        receipt))
                .isInstanceOf(DuplicateKeyException.class);
        assertThat(snapshot()).isEqualTo(published);
    }

    @Test
    void databaseRejectsIncompleteReportsRatherThanPersistingHalfAReceipt() {
        for (String column : List.of("id", "title", "action_hash", "result")) {
            var values =
                    new java.util.ArrayList<Object>(
                            List.of("report", "title", "action", "receipt"));
            values.set(List.of("id", "title", "action_hash", "result").indexOf(column), null);
            assertThatThrownBy(
                            () ->
                                    sql.update(
                                            "INSERT INTO business.reports(id,turn_id,call_id,title,action_hash,result) VALUES(?,'turn','call',?,?,?)",
                                            values.toArray()))
                    .as(column)
                    .isInstanceOf(DataIntegrityViolationException.class);
        }
        assertThat(snapshot()).isEmpty();
    }

    @Test
    void publicationReplaysTheFirstReceiptAndConflictingActionsCannotWriteAgain() {
        var leases = mock(ExecutionLeaseService.class);
        doAnswer(
                        invocation ->
                                tx.execute(
                                        status -> ((Supplier<?>) invocation.getArgument(1)).get()))
                .when(leases)
                .fenced(any(), any());
        var service =
                new ReportService(
                        new MyBatisReportGateway(mapper),
                        leases,
                        mock(ExecutionPolicyService.class));
        var claim =
                new Claim(
                        new Command("command", "turn", "running", "attempt", 1, true, null),
                        new Turn(
                                "turn",
                                "conversation",
                                "tenant",
                                "subject",
                                "key",
                                "request-hash",
                                "publish",
                                "release",
                                "running",
                                1,
                                null,
                                null));
        String first = service.report(claim, "call", "action", "首次发布");
        var published = snapshot();
        assertThat(service.report(claim, "call", "action", "重复调用的标题")).isEqualTo(first);
        assertThat(mapper.getReportReceipt("turn", "call").result()).isEqualTo(first);
        assertThatThrownBy(() -> service.report(claim, "call", "changed", "冲突标题"))
                .isInstanceOf(Blocked.class)
                .hasMessage("effect_conflict");
        assertThat(snapshot()).isEqualTo(published).hasSize(1);
    }

    private List<Map<String, Object>> snapshot() {
        return sql.queryForList("SELECT * FROM business.reports ORDER BY turn_id,call_id");
    }
}
