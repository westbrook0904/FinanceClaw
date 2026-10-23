package com.financeclaw.domain;

import com.financeclaw.domain.Task.*;
import java.util.*;

/** 证据记忆的领域约束：用户 epoch、画像版本、源消息、事实版本和后台任务租约共同决定数据是否仍可使用。 */
public final class Memory {
    private static final int MAX_EXTRACTION_PROPOSALS = 8;
    private static final int MAX_FACT_VALUE_CHARS = 256;
    private static final int MAX_EVIDENCE_CHARS = 1_024;

    private Memory() {}

    public record Privacy(
            String tenant, String subject, long epoch, boolean purgePending, long profileRevision) {
        public void requireAvailable() {
            if (purgePending) {
                throw new Problem(409, "purge_pending");
            }
        }

        public boolean permits(Source source) {
            return source != null && source.epoch() == epoch && !purgePending;
        }

        public boolean permits(long observedEpoch) {
            return epoch == observedEpoch && !purgePending;
        }

        public void requireEpoch(long expected) {
            if (epoch != expected) {
                throw new Problem(409, "memory_epoch_conflict");
            }
        }
    }

    public record Source(
            String id,
            String tenant,
            String subject,
            long epoch,
            String content,
            String status,
            long profileRevision) {
        public boolean extractable(Job job) {
            return content != null && status.equals("pending") && epoch == job.epoch();
        }

        public boolean pendingAt(long expectedEpoch) {
            return epoch == expectedEpoch && status.equals("pending");
        }
    }

    public record Fact(
            String id,
            String sourceId,
            String tenant,
            String subject,
            long epoch,
            String key,
            String content,
            String evidence,
            long revision,
            String status) {
        public void requireCandidate(Owner owner, Privacy privacy, long expectedRevision) {
            if (!tenant.equals(owner.tenant()) || !subject.equals(owner.subject())) {
                throw new Problem(404, "not_found");
            }
            if (privacy.purgePending()
                    || epoch != privacy.epoch()
                    || revision != expectedRevision
                    || !status.equals("candidate")) {
                throw new Problem(409, "stale_memory_candidate");
            }
        }

        public boolean activeAt(long expectedEpoch, long expectedRevision) {
            return status.equals("active")
                    && epoch == expectedEpoch
                    && revision == expectedRevision;
        }
    }

    public record Job(
            long id,
            String kind,
            String keyId,
            String tenant,
            String subject,
            long epoch,
            long revision,
            String modelRevision,
            String status,
            int attempts,
            String leaseToken) {
        public boolean live(Privacy privacy, boolean leaseLive) {
            return leaseLive
                    && privacy != null
                    && !privacy.purgePending()
                    && privacy.epoch() == epoch;
        }
    }

    public record Proposal(String key, String value, String quote) {
        public static Proposal explicit(String text) {
            if (text == null) {
                return null;
            }
            for (var entry :
                    Map.of(
                                    "response_language",
                                    Set.of("中文", "English"),
                                    "response_style",
                                    Set.of("简洁", "详细"))
                            .entrySet()) {
                for (var value : entry.getValue()) {
                    if (text.equals("/remember " + entry.getKey() + "=" + value)) {
                        return new Proposal(entry.getKey(), value, text);
                    }
                }
            }
            return null;
        }
    }

    public record Extraction(List<Proposal> proposals) {
        public void validateAgainst(Source source) {
            if (proposals == null || proposals.size() > MAX_EXTRACTION_PROPOSALS) {
                throw new IllegalArgumentException("invalid_extraction");
            }
            Set<String> keys = new HashSet<>();
            for (var p : proposals) {
                if (p == null
                        || !Set.of(
                                        "response_language",
                                        "response_style",
                                        "home_city",
                                        "travel_preference",
                                        "investment_preference")
                                .contains(p.key())
                        || !keys.add(p.key())
                        || p.value() == null
                        || p.value().isBlank()
                        || p.value().length() > MAX_FACT_VALUE_CHARS
                        || p.quote() == null
                        || p.quote().length() > MAX_EVIDENCE_CHARS
                        || !source.content().contains(p.quote())
                        || !p.quote().contains(p.value())) {
                    throw new IllegalArgumentException("untrusted_extraction");
                }
            }
        }
    }

    public record Operation(String requestHash, String result) {
        public String replay(String expectedHash) {
            if (!requestHash.equals(expectedHash)) {
                throw new Problem(409, "idempotency_conflict");
            }
            return result;
        }
    }
}
