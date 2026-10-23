package com.financeclaw.m0.support;

import com.financeclaw.domain.ExecutionScope;
import org.apache.ibatis.session.SqlSession;
import org.apache.ibatis.session.SqlSessionFactory;

/** M0 admission guard. One dedicated MyBatis session owns the advisory lock. */
public final class ExecutionGate implements AutoCloseable {
    private final SqlSession session;
    private final long key;

    private ExecutionGate(SqlSession session, long key) {
        this.session = session;
        this.key = key;
    }

    public static ExecutionGate acquire(SqlSessionFactory factory, ExecutionScope scope) {
        SqlSession session = factory.openSession(true);
        long key = Long.parseUnsignedLong(scope.key().substring(0, 16), 16);
        try {
            if (!session.getMapper(M0RuntimeMapper.class).tryLock(key))
                throw new IllegalStateException("Execution scope is already active");
            return new ExecutionGate(session, key);
        } catch (RuntimeException error) {
            session.close();
            throw error;
        }
    }

    @Override
    public void close() {
        try {
            if (!session.getMapper(M0RuntimeMapper.class).unlock(key))
                throw new IllegalStateException("Execution scope lock ownership was lost");
        } finally {
            session.close();
        }
    }
}
