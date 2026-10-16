import com.fasterxml.jackson.databind.ObjectMapper;
import io.agentscope.core.state.AgentState;
import io.agentscope.extensions.jdbc.dialect.vendor.SqliteDialect;
import io.agentscope.extensions.jdbc.state.JdbcAgentStateStore;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import javax.sql.DataSource;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.core.SpringVersion;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.TransactionAwareDataSourceProxy;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/** Isolated SQLite experiment; not a PostgreSQL or FinanceClaw recovery acceptance test. */
public final class SpringTransactionProbe {
  public interface ProbeMapper {
    @Insert("INSERT INTO tx_probe(id) VALUES(#{id})")
    int insert(@Param("id") String id);
  }

  private static final class Abort extends RuntimeException {}

  public static void main(String[] args) throws Exception {
    Path database = Files.createTempFile("financeclaw-spring-tx-", ".db");
    try {
      run(new DriverManagerDataSource("jdbc:sqlite:" + database), Path.of(args[0]));
    } finally {
      Files.deleteIfExists(database);
    }
  }

  private static void run(DataSource source, Path output) throws Exception {
    JdbcClient jdbc = JdbcClient.create(source);
    jdbc.sql("CREATE TABLE tx_probe(id TEXT PRIMARY KEY)").update();
    var factory = new SqlSessionFactoryBean();
    factory.setDataSource(source);
    var sessions = factory.getObject();
    sessions.getConfiguration().addMapper(ProbeMapper.class);
    var mapper = new SqlSessionTemplate(sessions).getMapper(ProbeMapper.class);
    var tx = new TransactionTemplate(new JdbcTransactionManager(source));
    var rawStore = new JdbcAgentStateStore(source, new SqliteDialect(), true);
    var proxiedStore =
        new JdbcAgentStateStore(new TransactionAwareDataSourceProxy(source), new SqliteDialect());
    Map<String, Object> checks = new LinkedHashMap<>();

    abort(
        tx,
        () -> {
          mapper.insert("spring-rollback-mybatis");
          insert(jdbc, "spring-rollback-jdbc");
        });
    require(count(jdbc, "spring-rollback-mybatis") == 0, "MyBatis insert must roll back");
    require(count(jdbc, "spring-rollback-jdbc") == 0, "JdbcClient insert must roll back");
    checks.put("spring_mybatis_and_jdbc_rollback", Map.of("remaining_rows", 0, "passed", true));

    tx.executeWithoutResult(
        status -> {
          mapper.insert("spring-commit-mybatis");
          insert(jdbc, "spring-commit-jdbc");
        });
    require(count(jdbc, "spring-commit-mybatis") == 1, "MyBatis insert must commit");
    require(count(jdbc, "spring-commit-jdbc") == 1, "JdbcClient insert must commit");
    checks.put("spring_mybatis_and_jdbc_commit", Map.of("remaining_rows", 2, "passed", true));

    abort(
        tx,
        () -> {
          // The SDK gets another connection and commits before the outer transaction writes.
          save(rawStore, "raw-sdk");
          insert(jdbc, "after-raw-sdk");
        });
    require(rawStore.exists("probe", "raw-sdk"), "Raw SDK state survives the outer rollback");
    require(count(jdbc, "after-raw-sdk") == 0, "The outer Spring insert rolls back");
    checks.put(
        "raw_sdk_does_not_join_spring",
        Map.of("sdk_state_survived", true, "outer_row_survived", false, "passed", true));

    abort(
        tx,
        () -> {
          mapper.insert("before-proxied-sdk");
          save(proxiedStore, "proxied-sdk");
          insert(jdbc, "after-proxied-sdk");
        });
    require(proxiedStore.exists("probe", "proxied-sdk"), "Proxied SDK state was committed early");
    require(count(jdbc, "before-proxied-sdk") == 1, "SDK commit also commits earlier business SQL");
    require(count(jdbc, "after-proxied-sdk") == 0, "Only subsequent business SQL rolls back");
    checks.put(
        "transaction_aware_proxy_does_not_suppress_sdk_commit",
        Map.of(
            "sdk_state_survived", true,
            "earlier_business_row_survived", true,
            "later_business_row_survived", false,
            "passed", true));

    var report = new LinkedHashMap<String, Object>();
    report.put("validation", "isolated_real_sqlite_jdbc_transactions");
    report.put("postgresql_acceptance", false);
    report.put("java", System.getProperty("java.runtime.version"));
    report.put("spring", SpringVersion.getVersion());
    report.put("agentscope_jdbc", "2.0.3");
    try (var connection = source.getConnection()) {
      report.put("sqlite_driver", connection.getMetaData().getDriverVersion());
      report.put("sqlite_database", connection.getMetaData().getDatabaseProductVersion());
    }
    report.put("checks", checks);
    new ObjectMapper().writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
    System.out.println("Passed " + checks.size() + " transaction experiments; report: " + output);
  }

  private static void save(JdbcAgentStateStore store, String session) {
    long version =
        store.saveIfVersion(
            "probe", session, "agent_state", AgentState.builder().sessionId(session).build(), 0);
    require(version == 1, "Expected a fresh SDK state");
  }

  private static void abort(TransactionTemplate tx, Runnable work) {
    try {
      tx.executeWithoutResult(
          status -> {
            work.run();
            throw new Abort();
          });
      throw new AssertionError("Expected rollback");
    } catch (Abort expected) {
      // Only the deliberate rollback is caught; SQL/SDK errors must fail the experiment.
    }
  }

  private static void insert(JdbcClient jdbc, String id) {
    jdbc.sql("INSERT INTO tx_probe(id) VALUES(:id)").param("id", id).update();
  }

  private static long count(JdbcClient jdbc, String id) {
    return jdbc.sql("SELECT count(*) FROM tx_probe WHERE id=:id")
        .param("id", id)
        .query(Long.class)
        .single();
  }

  private static void require(boolean condition, String message) {
    if (!condition) throw new AssertionError(message);
  }
}
