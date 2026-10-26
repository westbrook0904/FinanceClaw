package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.awaitility.Awaitility.await;

import com.financeclaw.api.TaskEvents;
import com.financeclaw.app.task.PersistentEventSignals;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.port.TaskEventAccess;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.runtime.PostgresEventListener;
import com.financeclaw.infrastructure.task.MyBatisTaskQueryGateway;
import com.financeclaw.infrastructure.task.TaskMapper;
import com.zaxxer.hikari.HikariDataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import javax.sql.DataSource;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DelegatingDataSource;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 两套独立连接池/LISTEN/socket/signals/hub 对接真实 MyBatis events SQL。 不模拟通知；由迁移安装的 PostgreSQL
 * 事务触发器发布。fixture 只设 owner+grant 读取门禁， 完整隐私/共享/发布门禁由 RuntimeCoordinationIT 覆盖；本类验证通知与连接生命周期。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaskEventNotificationIT {
    private static final Owner OWNER = new Owner("event-notification", "caller");
    private static final Duration WAIT = Duration.ofSeconds(8);
    private final PostgreSQLContainer<?> pg = new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Node> nodes = new ArrayList<>();
    private DataSource writer;
    private JdbcTemplate sql;

    @BeforeAll
    void startDatabase() {
        pg.start();
        writer = new DriverManagerDataSource(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
        Flyway.configure().dataSource(writer).load().migrate();
        sql = new JdbcTemplate(writer);
    }

    @BeforeEach
    void reset() {
        sql.execute("TRUNCATE business.conversations CASCADE");
    }

    @AfterEach
    void stopNodes() {
        for (var node : nodes) node.close();
        nodes.clear();
    }

    @AfterAll
    void stopDatabase() {
        pg.stop();
    }

    @Test
    void idleKeepalivesDoNotReadEventTableOrOpenExtraListenerConnections() throws Exception {
        Turn turn = admit();
        var node = node(100);
        try (var stream = node.open(turn, 0)) {
            await().atMost(WAIT).until(() -> stream.keepalives.get() >= 2);
            int before = stream.keepalives.get();
            var window = node.recorder.begin("idle-notification-stream");
            await().atMost(WAIT).until(() -> stream.keepalives.get() >= before + 4);
            var measurement = window.finish(WAIT);
            assertThat(measurement.count("event_read")).isZero();
            assertThat(measurement.count("grant_read"))
                    .as("keepalives still check current authority")
                    .isGreaterThanOrEqualTo(4);
            assertThat(node.signals.isConnected()).isTrue();
            assertThat(stream.result.isDone()).isFalse();
            await().atMost(WAIT)
                    .untilAsserted(
                            () ->
                                    assertThat(
                                                    node.pool
                                                            .getHikariPoolMXBean()
                                                            .getActiveConnections())
                                            .isOne());
        }
    }

    @Test
    void committedNotificationsWakeBothInstancesAndReplayHasNoDuplicateRevision() throws Exception {
        Turn turn = admit();
        var first = node(100);
        var second = node(100);
        assertThat(first.listener.backendPid()).isNotEqualTo(second.listener.backendPid());
        assertThat(first.hub).isNotSameAs(second.hub);
        try (var one = first.open(turn, 0);
                var two = second.open(turn, 0)) {
            var window = second.recorder.begin("cross-instance-notification");
            // The writer has no reference to either local hub/signals; only PG can wake the
            // streams.
            event(turn, 2, "running");
            await().atMost(WAIT)
                    .until(() -> one.revisions().contains("2") && two.revisions().contains("2"));
            event(turn, 3, "completed");
            assertThat(one.result.get(8, TimeUnit.SECONDS))
                    .extracting(ServerSentEvent::id)
                    .containsExactly("1", "2", "3");
            assertThat(two.result.get(8, TimeUnit.SECONDS))
                    .extracting(ServerSentEvent::id)
                    .containsExactly("1", "2", "3");
            assertThat(window.finish(WAIT).count("event_read")).isGreaterThanOrEqualTo(2);
        }
        try (var replay = second.open(turn, 1)) {
            assertThat(replay.result.get(8, TimeUnit.SECONDS))
                    .extracting(ServerSentEvent::id)
                    .containsExactly("2", "3");
        }
    }

    @Test
    void rolledBackEventDoesNotWakeOrAdvanceAStream() throws Exception {
        Turn turn = admit();
        var node = node(100);
        try (var stream = node.open(turn, 0);
                var pending = writer.getConnection()) {
            pending.setAutoCommit(false);
            writeEvent(pending, turn, 2, "running");
            int before = stream.keepalives.get();
            var window = node.recorder.begin("uncommitted-event");
            await().atMost(WAIT).until(() -> stream.keepalives.get() >= before + 3);
            pending.rollback();
            assertThat(window.finish(WAIT).count("event_read")).isZero();
            assertThat(stream.revisions()).containsExactly("1");
            event(turn, 2, "completed");
            assertThat(stream.result.get(8, TimeUnit.SECONDS))
                    .extracting(ServerSentEvent::id)
                    .containsExactly("1", "2");
        }
    }

    @Test
    void grantNotificationClosesIdleStreamBeforeTheNextKeepalive() throws Exception {
        Turn turn = admit();
        var node = node(100);
        // A 60s keepalive makes notification, rather than a timer tick, necessary to pass the 8s
        // deadline.
        var slowKeepalive = node.events(60000);
        try (var stream = open(slowKeepalive, turn, 0)) {
            sql.update(
                    "UPDATE business.grants SET revision=revision+1,scopes='[]'::jsonb WHERE conversation_id=?",
                    turn.conversationId());
            assertThatThrownBy(() -> stream.result.get(8, TimeUnit.SECONDS))
                    .hasRootCauseMessage("authorization_changed");
            assertThat(stream.revisions()).containsExactly("1");
            assertThat(stream.keepalives).hasValue(0);
        }
    }

    @Test
    void listenerDisconnectFailsOldSubscriptionsAndReconnectedStreamReplaysGapThenWakesAgain()
            throws Exception {
        Turn turn = admit();
        var node = node(100);
        long generation = node.signals.generation();
        int oldPid = node.listener.backendPid();
        try (var old = node.open(turn, 0)) {
            node.listenerSource.pauseNewConnections();
            assertThat(sql.queryForObject("SELECT pg_terminate_backend(?)", Boolean.class, oldPid))
                    .isTrue();
            assertThatThrownBy(() -> old.result.get(8, TimeUnit.SECONDS))
                    .hasRootCauseMessage("persistent_events_unavailable");
            assertThat(old.revisions()).containsExactly("1");
            assertThat(node.signals.isConnected()).isFalse();
            // This commit intentionally occurs while no LISTEN session exists.
            event(turn, 2, "running");
            assertThatThrownBy(() -> node.signals.subscribe(turn.id()))
                    .hasMessage("persistent_events_unavailable");
            node.listenerSource.resumeNewConnections();
            await().atMost(Duration.ofSeconds(12))
                    .until(
                            () ->
                                    node.signals.isConnected()
                                            && node.listener.backendPid() > 0
                                            && node.listener.backendPid() != oldPid);
            assertThat(node.signals.generation()).isGreaterThan(generation);
            try (var resumed = node.open(turn, 1)) {
                await().atMost(WAIT).until(() -> resumed.revisions().contains("2"));
                event(turn, 3, "completed");
                assertThat(resumed.result.get(8, TimeUnit.SECONDS))
                        .extracting(ServerSentEvent::id)
                        .containsExactly("2", "3");
                assertThat(old.revisions()).containsExactly("1");
            }
        }
    }

    @Test
    void listenerCloseTerminatesBackendReturnsPoolSlotAndRejectsNewSubscriptions()
            throws Exception {
        Turn turn = admit();
        var node = node(100);
        int backend = node.listener.backendPid();
        try (var stream = node.open(turn, 0)) {
            node.listener.close();
            assertThatThrownBy(() -> stream.result.get(8, TimeUnit.SECONDS))
                    .hasRootCauseMessage("persistent_events_unavailable");
            await().atMost(WAIT)
                    .untilAsserted(
                            () -> {
                                assertThat(node.listener.backendPid()).isZero();
                                assertThat(node.pool.getHikariPoolMXBean().getActiveConnections())
                                        .isZero();
                                assertThat(
                                                sql.queryForObject(
                                                        "SELECT count(*) FROM pg_stat_activity WHERE pid=?",
                                                        Long.class,
                                                        backend))
                                        .isZero();
                            });
            assertThat(node.signals.isConnected()).isFalse();
            assertThatThrownBy(() -> node.signals.subscribe(turn.id()))
                    .hasMessage("persistent_events_unavailable");
            // Remaining business queries still work: shutdown releases only the listener's
            // connection.
            assertThat(node.queries.get(OWNER, turn.conversationId(), turn.id()).id())
                    .isEqualTo(turn.id());
        }
    }

    private Node node(long reconnectMillis) throws Exception {
        var node = new Node(reconnectMillis);
        nodes.add(node);
        node.listener.start();
        await().atMost(WAIT)
                .until(() -> node.signals.isConnected() && node.listener.backendPid() > 0);
        return node;
    }

    private final class Node implements AutoCloseable {
        final HikariDataSource pool = new HikariDataSource();
        final PersistentEventSignals signals = new PersistentEventSignals();
        final TaskEventHub hub = new TaskEventHub();
        final ApplicationJdbcRecorder recorder = new ApplicationJdbcRecorder();
        final TaskQueryService queries;
        final TaskEventAccess gate;
        final PostgresEventListener listener;
        final ReconnectBarrier listenerSource;

        Node(long reconnectMillis) throws Exception {
            pool.setJdbcUrl(pg.getJdbcUrl());
            pool.setUsername(pg.getUsername());
            pool.setPassword(pg.getPassword());
            pool.setPoolName("event-notification-" + UUID.randomUUID());
            pool.setMaximumPoolSize(4);
            pool.setMinimumIdle(0);
            pool.setConnectionTimeout(2000);
            var observed = recorder.wrap(pool);
            var configuration = new Configuration();
            configuration.setMapUnderscoreToCamelCase(true);
            configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
            configuration.setCacheEnabled(false);
            configuration.addMapper(TaskMapper.class);
            var factory = new SqlSessionFactoryBean();
            factory.setDataSource(observed);
            factory.setConfiguration(configuration);
            var gateway =
                    new MyBatisTaskQueryGateway(
                            new SqlSessionTemplate(factory.getObject())
                                    .getMapper(TaskMapper.class));
            queries = new TaskQueryService(gateway);
            var authority = new JdbcTemplate(observed);
            var tx = new TransactionTemplate(new DataSourceTransactionManager(observed));
            gate =
                    turn ->
                            tx.executeWithoutResult(
                                    status -> {
                                        var current =
                                                gateway.ownedTurn(
                                                        OWNER, turn.conversationId(), turn.id());
                                        if (current == null) throw new Problem(404, "not_found");
                                        Long revision =
                                                authority.queryForObject(
                                                        "SELECT revision FROM business.grants WHERE conversation_id=? FOR SHARE",
                                                        Long.class,
                                                        turn.conversationId());
                                        Long frozen =
                                                authority.queryForObject(
                                                        "SELECT grant_revision FROM business.turn_policies WHERE turn_id=?",
                                                        Long.class,
                                                        turn.id());
                                        if (!Objects.equals(revision, frozen))
                                            throw new Problem(403, "authorization_changed");
                                    });
            // The blocking LISTEN connection is outside the SQL window; the real replay/query path
            // is observed.
            listenerSource = new ReconnectBarrier(pool);
            listener = new PostgresEventListener(listenerSource, signals, 100, reconnectMillis);
        }

        TaskEvents events(long heartbeatMillis) {
            return new TaskEvents(queries, hub, 8, 30000, heartbeatMillis, 64, gate, signals);
        }

        OpenStream open(Turn turn, long after) throws Exception {
            return TaskEventNotificationIT.open(events(100), turn, after);
        }

        @Override
        public void close() {
            listenerSource.resumeNewConnections();
            listener.close();
            pool.close();
        }
    }

    /**
     * Hold only replacement borrows so the disconnected commit gap does not depend on scheduler
     * timing.
     */
    private static final class ReconnectBarrier extends DelegatingDataSource {
        private volatile CountDownLatch pause;

        ReconnectBarrier(DataSource source) {
            super(source);
        }

        void pauseNewConnections() {
            pause = new CountDownLatch(1);
        }

        void resumeNewConnections() {
            var old = pause;
            pause = null;
            if (old != null) old.countDown();
        }

        @Override
        public Connection getConnection() throws SQLException {
            var current = pause;
            if (current != null) {
                try {
                    if (!current.await(15, TimeUnit.SECONDS))
                        throw new SQLException("fixture_reconnect_barrier_timeout");
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw new SQLException("fixture_reconnect_interrupted");
                }
            }
            return super.getConnection();
        }
    }

    private static final class OpenStream implements AutoCloseable {
        final List<ServerSentEvent<Event>> delivered = new CopyOnWriteArrayList<>();
        final AtomicInteger keepalives = new AtomicInteger();
        CompletableFuture<List<ServerSentEvent<Event>>> result;

        List<String> revisions() {
            return delivered.stream().map(ServerSentEvent::id).toList();
        }

        @Override
        public void close() {
            result.cancel(true);
        }
    }

    private static OpenStream open(TaskEvents events, Turn turn, long after) throws Exception {
        var stream = new OpenStream();
        var first = new CountDownLatch(1);
        stream.result =
                events.open(OWNER, turn.conversationId(), turn.id(), after)
                        .doOnNext(
                                frame -> {
                                    if (frame.data() == null) stream.keepalives.incrementAndGet();
                                })
                        .filter(frame -> frame.data() != null)
                        .doOnNext(
                                frame -> {
                                    stream.delivered.add(frame);
                                    first.countDown();
                                })
                        .collectList()
                        .toFuture();
        if (!first.await(8, TimeUnit.SECONDS)) {
            stream.close();
            throw new AssertionError("initial durable replay did not arrive");
        }
        return stream;
    }

    private Turn admit() {
        String conversation = UUID.randomUUID().toString(), id = UUID.randomUUID().toString();
        new TransactionTemplate(new DataSourceTransactionManager(writer))
                .executeWithoutResult(
                        status -> {
                            sql.update(
                                    "INSERT INTO business.conversations(id,tenant,subject,release_hash) VALUES(?,?,?,?)",
                                    conversation,
                                    OWNER.tenant(),
                                    OWNER.subject(),
                                    "a".repeat(64));
                            sql.update(
                                    "INSERT INTO business.grants(conversation_id,revision,scopes) VALUES(?,1,'[]'::jsonb)",
                                    conversation);
                            sql.update(
                                    "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status,revision) VALUES(?,?,?,?,?,?,?,?,'queued',1)",
                                    id,
                                    conversation,
                                    OWNER.tenant(),
                                    OWNER.subject(),
                                    "request",
                                    "b".repeat(64),
                                    "test-input",
                                    "a".repeat(64));
                            sql.update(
                                    "INSERT INTO business.turn_policies(turn_id,grant_revision,scopes) VALUES(?,1,'[]'::jsonb)",
                                    id);
                            sql.update(
                                    "INSERT INTO business.events(turn_id,revision,status) VALUES(?,1,'queued')",
                                    id);
                        });
        return new Turn(
                id,
                conversation,
                OWNER.tenant(),
                OWNER.subject(),
                "request",
                "b".repeat(64),
                "test-input",
                "a".repeat(64),
                "queued",
                1,
                null,
                null);
    }

    private void event(Turn turn, long revision, String status) throws Exception {
        try (var connection = writer.getConnection()) {
            connection.setAutoCommit(false);
            try {
                writeEvent(connection, turn, revision, status);
                connection.commit();
            } catch (Exception failed) {
                connection.rollback();
                throw failed;
            }
        }
    }

    private static void writeEvent(Connection connection, Turn turn, long revision, String status)
            throws Exception {
        try (var update =
                        connection.prepareStatement(
                                "UPDATE business.turns SET revision=?,status=? WHERE id=?");
                var event =
                        connection.prepareStatement(
                                "INSERT INTO business.events(turn_id,revision,status) VALUES(?,?,?)")) {
            update.setLong(1, revision);
            update.setString(2, status);
            update.setString(3, turn.id());
            update.executeUpdate();
            event.setString(1, turn.id());
            event.setLong(2, revision);
            event.setString(3, status);
            event.executeUpdate();
        }
    }
}
