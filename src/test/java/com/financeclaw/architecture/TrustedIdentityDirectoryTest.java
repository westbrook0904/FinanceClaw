package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.api.TrustedIdentityDirectory;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.agent.ServerCredentialCatalog;
import org.junit.jupiter.api.Test;

class TrustedIdentityDirectoryTest {
    @Test
    void distinctTokensBindFixedOwnersAndOnlyExplicitRolesHavePermissions() {
        var directory =
                new TrustedIdentityDirectory(
                        "legacy",
                        "local",
                        "developer",
                        """
            [{"token":"caller","tenant":"t","subject":"reader","roles":[]},
             {"token":"manager","tenant":"t","subject":"publisher","roles":["agent_manager","agent_sharer","scheduler_manager","runtime_manager"]}]
            """);
        assertThat(directory.authenticate("legacy").permissions()).isEmpty();
        assertThat(directory.authenticate("caller").owner()).isEqualTo(new Owner("t", "reader"));
        assertThat(directory.authenticate("caller").permissions()).isEmpty();
        assertThat(directory.authenticate("manager").permissions())
                .containsExactlyInAnyOrder(
                        "agent:manage", "agent:share", "schedules:manage", "runtime:manage");
        assertThat(directory.authenticate("wrong")).isNull();
        assertThat(directory.authenticate("caller ")).isNull();
        assertThat(directory.authenticate(null)).isNull();
        assertThat(directory.authenticate("x".repeat(4097))).isNull();
    }

    @Test
    void invalidAndAmbiguousDirectoriesFailClosedWithoutEchoingSecrets() {
        for (String value :
                new String[] {
                    "{}",
                    "[] {}",
                    "[{\"token\":\"SENSITIVE\",\"tenant\":\"t\",\"subject\":\"s\",\"roles\":[\"admin\"]}]",
                    "[{\"token\":\"SENSITIVE\",\"tenant\":\"t\",\"subject\":\"s\",\"roles\":[],\"admin\":true}]",
                    "[{\"token\":\"SENSITIVE\",\"token\":\"other\",\"tenant\":\"t\",\"subject\":\"s\",\"roles\":[]}]",
                    "[{\"token\":\"SENSITIVE\",\"tenant\":\"t\",\"subject\":\"s\",\"roles\":[]}]"
                }) {
            assertThatThrownBy(
                            () ->
                                    new TrustedIdentityDirectory(
                                            "SENSITIVE", "local", "developer", value))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessage("Invalid trusted identity directory")
                    .hasNoCause();
        }
    }

    @Test
    void credentialCatalogRejectsUncontrolledReferencesAndHasOwnerScopedLegacyCompatibility() {
        var owner = new Owner("local", "developer");
        var legacy = new ServerCredentialCatalog("[]", owner, "[]");
        assertThat(legacy.permitsLegacy(owner)).isTrue();
        assertThat(legacy.permitsLegacy(new Owner("local", "another"))).isFalse();
        var multi = new ServerCredentialCatalog("[]", owner, "[{}]");
        assertThat(multi.permitsLegacy(owner)).isFalse();
        var catalog =
                new ServerCredentialCatalog(
                        """
            [{"resourceId":"model","reference":"@model","tenant":"t","subject":"s"},
             {"resourceId":"search","reference":"SEARCH_TOKEN","tenant":"t","subject":"s"}]
            """,
                        owner,
                        "[]");
        assertThat(catalog.owned(new Owner("t", "s"))).hasSize(2);
        assertThat(catalog.owned(owner)).isEmpty();
        assertThat(catalog.permitsLegacy(owner)).isFalse();
        assertThat(catalog.resource("search").bindingHash()).hasSize(64);
        for (String reference :
                new String[] {"${TOKEN}", "http://secret", "lowercase", "SECRET VALUE"}) {
            String config =
                    "[{\"resourceId\":\"x\",\"reference\":\""
                            + reference
                            + "\",\"tenant\":\"t\",\"subject\":\"s\"}]";
            assertThatThrownBy(() -> new ServerCredentialCatalog(config, owner, "[]"))
                    .hasMessage("Invalid credential resource catalog")
                    .hasNoCause();
        }
    }
}
