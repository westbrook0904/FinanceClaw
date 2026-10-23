package com.financeclaw.m0.support;

import com.financeclaw.domain.ExecutionScope;
import java.util.Optional;

/** M0 safety experiment: a lost tool receipt is blocked until externally reconciled. */
public final class EffectLedger {
    private final M0RuntimeMapper mapper;
    private final ExecutionScope scope;

    public EffectLedger(M0RuntimeMapper mapper, ExecutionScope scope) {
        this.mapper = mapper;
        this.scope = scope;
    }

    public Optional<String> begin(String operation, String requestHash) {
        if (mapper.beginEffect(scope.key(), operation, requestHash) == 1) return Optional.empty();
        var row = mapper.findEffect(scope.key(), operation);
        if (row == null) throw new IllegalStateException("Missing effect receipt");
        if (!requestHash.equals(row.requestHash()))
            throw new IllegalArgumentException("Operation key reused with different input");
        if (!"completed".equals(row.status())) throw new UncertainEffectException();
        return Optional.of(row.resultJson());
    }

    public void complete(String operation, String requestHash, String verifiedResultJson) {
        if (mapper.completeEffect(scope.key(), operation, requestHash, verifiedResultJson) != 1)
            throw new IllegalStateException("No matching pending operation to reconcile");
    }

    public void assertNoUncertainEffects() {
        if (mapper.hasPendingEffect(scope.key())) throw new UncertainEffectException();
    }

    public static final class UncertainEffectException extends IllegalStateException {
        public UncertainEffectException() {
            super("blocked: external tool outcome requires reconciliation");
        }
    }
}
