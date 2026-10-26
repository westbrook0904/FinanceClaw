package com.financeclaw.support;

import com.financeclaw.domain.Governance.Blocked;
import java.util.Collection;
import java.util.List;
import java.util.TreeSet;

/** Persist a conjunction of source permissions without reducing a mixed result to one scope. */
public final class ScopeRequirements {
    private ScopeRequirements() {}

    public static String encode(Collection<String> scopes) {
        var sorted = new TreeSet<>(scopes);
        if (sorted.isEmpty()) sorted.add("artifact:read");
        return sorted.size() == 1 ? sorted.first() : "all:" + PayloadJson.canonical(sorted);
    }

    public static List<String> decode(String value) {
        if (value == null || value.isBlank()) throw new Blocked("artifact_scope_denied");
        var scopes =
                value.startsWith("all:") ? PayloadJson.scopes(value.substring(4)) : List.of(value);
        if (scopes.isEmpty()
                || scopes.stream()
                        .anyMatch(
                                scope ->
                                        scope == null
                                                || !scope.matches(
                                                        "[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*")))
            throw new Blocked("artifact_scope_denied");
        return scopes;
    }

    public static boolean allowed(Collection<String> granted, String requirement) {
        return granted != null && granted.containsAll(decode(requirement));
    }
}
