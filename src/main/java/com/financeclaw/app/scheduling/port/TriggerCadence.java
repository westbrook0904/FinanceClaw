package com.financeclaw.app.scheduling.port;

import java.time.Instant;

/** Cron calculation is supplied by the infrastructure adapter, including zone/DST semantics. */
public interface TriggerCadence {
    Instant next(String expression, String zone, Instant after);
}
