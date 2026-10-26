package com.financeclaw.domain.scheduling;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import java.time.Instant;
import java.util.Set;
import java.util.UUID;

/**
 * Immutable rule revision; each accepted Turn records its own release and normal execution
 * identity.
 */
public record TriggerRule(
        String id,
        String tenant,
        String subject,
        long revision,
        boolean enabled,
        String kind,
        String expression,
        String zone,
        String misfire,
        String overlap,
        String agentId,
        String versionPolicy,
        String releaseHash,
        String message,
        int priority,
        Instant nextAt,
        Instant retryAfter,
        String lastError) {
    public Owner owner() {
        return new Owner(tenant, subject);
    }

    public static void requireId(String value) {
        try {
            if (value == null || !UUID.fromString(value).toString().equals(value))
                throw new IllegalArgumentException();
        } catch (IllegalArgumentException bad) {
            throw new Problem(422, "invalid_schedule_id");
        }
    }

    public record Definition(
            boolean enabled,
            String kind,
            String expression,
            String zone,
            String misfire,
            String overlap,
            String agentId,
            String versionPolicy,
            String message,
            int priority) {
        public void validate() {
            if (!Set.of("cron", "webhook").contains(kind == null ? "" : kind)
                    || !Set.of("skip", "fire_once", "catch_up")
                            .contains(misfire == null ? "" : misfire)
                    || !Set.of("allow", "skip").contains(overlap == null ? "" : overlap)
                    || !Set.of("fixed", "current")
                            .contains(versionPolicy == null ? "" : versionPolicy)
                    || agentId == null
                    || agentId.isBlank()
                    || agentId.length() > 128
                    || message == null
                    || message.isBlank()
                    || message.length() > 32000
                    || priority < -100
                    || priority > 100
                    || (kind.equals("cron")
                            && (expression == null
                                    || expression.length() > 256
                                    || zone == null
                                    || zone.length() > 128))
                    || (kind.equals("webhook") && (expression != null || zone != null))) {
                throw new Problem(422, "invalid_schedule");
            }
        }
    }
}
