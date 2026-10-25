package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.util.List;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;

/** 只校准观察协议的透明性；业务数据、真实列宽与 PostgreSQL 路径另由 NoticeCandidateQueryIT 验收。 */
class NoticeResultSetRecorderTest {
    private static final String OLD_SQL =
            "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason "
                    + "FROM channel.notices WHERE status='pending' ORDER BY created_at LIMIT 1";
    private static final String NEW_SQL =
            "SELECT id,binding_id FROM channel.notices WHERE status='pending' ORDER BY created_at LIMIT 1";

    @Test
    void executeThenRepeatedResultSetAndGetterAccessNeverReadsAheadOrCountsACellTwice()
            throws Exception {
        var source = mock(DataSource.class);
        var connection = mock(Connection.class);
        var statement = mock(PreparedStatement.class);
        var rows = mock(ResultSet.class);
        var metadata =
                metadata(
                        "id",
                        "turn_id",
                        "binding_id",
                        "binding_revision",
                        "revision",
                        "payload",
                        "status",
                        "reason");
        when(source.getConnection()).thenReturn(connection);
        when(connection.prepareStatement(OLD_SQL)).thenReturn(statement);
        when(statement.execute()).thenReturn(true);
        when(statement.getResultSet()).thenReturn(rows);
        when(rows.getMetaData()).thenReturn(metadata);
        when(rows.next()).thenReturn(true, false);
        when(rows.getString("payload")).thenReturn("证据🙂");
        when(rows.getString(6)).thenReturn("证据🙂");
        var recorder = new NoticeResultSetRecorder();
        recorder.begin("execute-protocol");
        try (var wrapped = recorder.wrap(source).getConnection();
                var query = wrapped.prepareStatement(OLD_SQL)) {
            assertThat(query.execute()).isTrue();
            assertThat(query.getConnection()).isSameAs(wrapped);
            var first = query.getResultSet();
            assertThat(first.getStatement()).isSameAs(query);
            assertThat(query.getResultSet()).isSameAs(first);
            // 取 metadata 与重复 getResultSet 都不得偷偷推进游标或额外读正文。
            verify(rows, never()).next();
            verify(rows, never()).getString(anyString());
            verify(rows, never()).getString(anyInt());
            assertThat(first.next()).isTrue();
            assertThat(first.getString("payload")).isEqualTo("证据🙂");
            assertThat(first.getString(6)).isEqualTo("证据🙂");
            assertThat(first.next()).isFalse();
        }
        var observed = recorder.finish().results();
        assertThat(observed).hasSize(1);
        var result = observed.getFirst();
        assertThat(result.columns())
                .containsExactly(
                        "id",
                        "turn_id",
                        "binding_id",
                        "binding_revision",
                        "revision",
                        "payload",
                        "status",
                        "reason");
        assertThat(result.resultSetObserved()).isTrue();
        assertThat(result.rows()).isOne();
        assertThat(result.payloadCellsRead()).isOne();
        assertThat(result.payloadUtf8Bytes())
                .isEqualTo("证据🙂".getBytes(StandardCharsets.UTF_8).length);
        verify(statement).execute();
        verify(statement, times(2)).getResultSet();
        verify(rows, times(2)).next();
        verify(rows).getString("payload");
        verify(rows).getString(6);
        verify(rows, never()).getObject(anyString());
        verify(metadata).getColumnCount();
        verify(rows).getMetaData();
    }

    @Test
    void executeQueryWithNoRowsStillRecordsColumnsWithoutAnyPayloadGetter() throws Exception {
        var source = mock(DataSource.class);
        var connection = mock(Connection.class);
        var statement = mock(PreparedStatement.class);
        var rows = mock(ResultSet.class);
        when(source.getConnection()).thenReturn(connection);
        when(connection.prepareStatement(NEW_SQL)).thenReturn(statement);
        when(statement.executeQuery()).thenReturn(rows);
        var columns = metadata("id", "binding_id");
        when(rows.getMetaData()).thenReturn(columns);
        when(rows.next()).thenReturn(false);
        var recorder = new NoticeResultSetRecorder();
        recorder.begin("empty-query-protocol");
        try (var wrapped = recorder.wrap(source).getConnection();
                var query = wrapped.prepareStatement(NEW_SQL);
                var result = query.executeQuery()) {
            verify(rows, never()).next();
            assertThat(result.next()).isFalse();
        }
        var result = recorder.finish().results().getFirst();
        assertThat(result.kind()).isEqualTo("candidate");
        assertThat(result.templateSha256()).isEqualTo(NoticeResultSetRecorder.NEW_CANDIDATE);
        assertThat(result.resultSetObserved()).isTrue();
        assertThat(result.columns()).isEqualTo(List.of("id", "binding_id"));
        assertThat(result.rows()).isZero();
        assertThat(result.payloadCellsRead()).isZero();
        assertThat(result.payloadUtf8Bytes()).isZero();
        verify(statement).executeQuery();
        verify(rows).next();
        verify(rows, never()).getString(anyString());
        verify(rows, never()).getString(anyInt());
    }

    private ResultSetMetaData metadata(String... labels) throws Exception {
        var metadata = mock(ResultSetMetaData.class);
        when(metadata.getColumnCount()).thenReturn(labels.length);
        for (int i = 0; i < labels.length; i++)
            when(metadata.getColumnLabel(i + 1)).thenReturn(labels[i]);
        return metadata;
    }
}
