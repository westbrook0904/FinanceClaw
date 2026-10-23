package com.financeclaw.worker;

import static org.junit.jupiter.api.Assertions.*;

import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;

class IdlePollTest {
    private final AtomicLong now = new AtomicLong();
    private final IdlePoll idle = new IdlePoll(250, 5000, now::get);

    @Test
    void repeatedEmptyQueriesBackOffExponentiallyAndStopAtFiveSeconds() {
        for (long delay : new long[] {250, 500, 1000, 2000, 4000, 5000, 5000}) {
            var ticket = idle.acquire(0);
            assertNotNull(ticket);
            idle.empty(ticket);
            advance(delay - 1);
            assertNull(idle.acquire(0));
            advance(1);
            assertNotNull(idle.acquire(0));
        }
    }

    @Test
    void commitBetweenEmptyQueryAndIdleRecordingWakesTheNextTick() {
        var query = idle.acquire(11);
        // The SQL query saw no work, then a command committed before empty() ran.
        idle.empty(query);
        assertNotNull(idle.acquire(12));
        idle.empty(query); // An older query cannot overwrite the newly observed generation either.
        assertNotNull(idle.acquire(12));
    }

    @Test
    void capacityReleaseBetweenQueryAndIdleRecordingCannotBeLost() {
        var query = idle.acquire(0);
        idle.wake();
        idle.empty(query);
        assertNotNull(idle.acquire(0));
    }

    @Test
    void realWorkResetsTheNextEmptyDelayWithoutInvalidatingItsTicket() {
        idle.empty(idle.acquire(0));
        advance(250);
        idle.empty(idle.acquire(0));
        advance(500);
        var ticket = idle.acquire(0);
        idle.worked();
        idle.empty(ticket);
        advance(249);
        assertNull(idle.acquire(0));
        advance(1);
        assertNotNull(idle.acquire(0));
    }

    @Test
    void elapsedTimeComparisonHandlesMonotonicClockOverflow() {
        now.set(Long.MAX_VALUE - 100);
        idle.empty(idle.acquire(0));
        advance(249);
        assertNull(idle.acquire(0));
        advance(1);
        assertNotNull(idle.acquire(0));
    }

    private void advance(long millis) {
        now.addAndGet(TimeUnit.MILLISECONDS.toNanos(millis));
    }
}
