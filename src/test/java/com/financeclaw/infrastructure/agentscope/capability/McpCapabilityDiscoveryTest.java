package com.financeclaw.infrastructure.agentscope.capability;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.tools.McpTools;
import java.time.Duration;
import java.util.*;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.support.TransactionSynchronizationManager;

class McpCapabilityDiscoveryTest {
    private static final Owner OWNER = new Owner("tenant", "subject");
    private static final List<String> SELECTED = List.of("mcp:hotel/search");

    @Test
    void publicationOnlyListsSelectedServiceAndReadsOnlyItsCredential() throws Exception {
        try (var hotel = new McpContractFixture();
                var unused = new McpContractFixture()) {
            var credentials = mock(Credentials.class);
            when(credentials.resolve("hotel-token")).thenReturn("opaque");
            var discovery = new McpCapabilityDiscovery(credentials);
            var result =
                    discovery.discover(
                            OWNER,
                            McpContractFixture.release(
                                    hotel.remote("hotel", "hotel-token"),
                                    unused.remote("taibu", "unused-token")),
                            SELECTED);
            assertThat(result).hasSize(1);
            var binding = result.getFirst();
            assertThat(binding.id()).isEqualTo("mcp:hotel/search");
            assertThat(binding.scopes()).containsExactly("travel:read");
            assertThat(binding.dependencies()).containsExactly("function:read_artifact_range");
            assertThat(binding.contentHash())
                    .isEqualTo(PayloadDigest.sha256(binding.contractJson()));
            assertThat(binding.contractJson())
                    .contains(
                            "mcp-sdk-effective/1",
                            "outputSchema",
                            "\"description\":{\"type\":\"string\"}",
                            "readOnly");
            assertThat(hotel.lists.get()).isGreaterThanOrEqualTo(1);
            assertThat(hotel.calls.get()).isZero();
            assertThat(unused.lists.get()).isZero();
            assertThat(unused.calls.get()).isZero();
            verify(credentials, atLeastOnce()).resolve("hotel-token");
            verify(credentials, never()).resolve("unused-token");
            verify(credentials, never()).modelApiKey();
        }
    }

    @Test
    void selectingNoMcpRequiresNeitherConnectionNorCredentials() throws Exception {
        try (var remote = new McpContractFixture()) {
            var credentials = mock(Credentials.class);
            var result =
                    new McpCapabilityDiscovery(credentials)
                            .discover(
                                    OWNER,
                                    McpContractFixture.release(remote.remote("hotel", "token")),
                                    List.of("function:query_catalog"));
            assertThat(result).isEmpty();
            assertThat(remote.lists.get()).isZero();
            verifyNoInteractions(credentials);
        }
    }

    @Test
    void effectiveInputOutputAndDescriptionChangesAllProduceNewHashes() throws Exception {
        try (var remote = new McpContractFixture()) {
            var discovery = discovery();
            var release = McpContractFixture.release(remote.remote("hotel", ""));
            var first = discovery.discover(OWNER, release, SELECTED).getFirst();
            remote.tools = List.of(McpContractFixture.tool("search", 200, "string"));
            var input = discovery.discover(OWNER, release, SELECTED).getFirst();
            remote.tools = List.of(McpContractFixture.tool("search", 200, "integer"));
            var output = discovery.discover(OWNER, release, SELECTED).getFirst();
            var changed = new LinkedHashMap<>(remote.tools.getFirst());
            changed.put("description", "Changed wording");
            remote.tools = List.of(changed);
            var description = discovery.discover(OWNER, release, SELECTED).getFirst();
            assertThat(
                            Set.of(
                                    first.contentHash(),
                                    input.contentHash(),
                                    output.contentHash(),
                                    description.contentHash()))
                    .hasSize(4);
            assertThat(remote.calls.get()).isZero();
        }
    }

    @Test
    void missingAndDuplicateToolsCannotBeFrozen() throws Exception {
        try (var remote = new McpContractFixture()) {
            var release = McpContractFixture.release(remote.remote("hotel", ""));
            remote.tools = List.of(McpContractFixture.tool("unused", 100, "string"));
            assertThatThrownBy(() -> discovery().discover(OWNER, release, SELECTED))
                    .isInstanceOf(Blocked.class)
                    .hasMessage("mcp_tool_unavailable");
            remote.tools =
                    List.of(
                            McpContractFixture.tool("search", 100, "string"),
                            McpContractFixture.tool("search", 200, "string"));
            assertThatThrownBy(() -> discovery().discover(OWNER, release, SELECTED))
                    .isInstanceOf(Blocked.class)
                    .hasMessage("mcp_contract_invalid");
            assertThat(remote.calls.get()).isZero();
        }
    }

    @Test
    void boundsApplyToEntireDiscoveredDirectoryNotOnlySelectedTool() throws Exception {
        try (var remote = new McpContractFixture()) {
            var release = McpContractFixture.release(remote.remote("hotel", ""));
            var limited =
                    new McpCapabilityDiscovery(
                            credentials(),
                            new McpTools.DiscoveryLimits(
                                    1, 262144, 1048576, Duration.ofSeconds(5)));
            assertThatThrownBy(() -> limited.discover(OWNER, release, SELECTED))
                    .isInstanceOf(Blocked.class)
                    .hasMessage("mcp_discovery_limit_exceeded");
            var byteLimited =
                    new McpCapabilityDiscovery(
                            credentials(),
                            new McpTools.DiscoveryLimits(10, 100, 1000, Duration.ofSeconds(5)));
            assertThatThrownBy(() -> byteLimited.discover(OWNER, release, SELECTED))
                    .isInstanceOf(Blocked.class)
                    .hasMessage("mcp_discovery_limit_exceeded");
            assertThat(remote.calls.get()).isZero();
        }
    }

    @Test
    void overallDiscoveryTimeoutIsBoundedAndDoesNotCallTools() throws Exception {
        try (var remote = new McpContractFixture()) {
            remote.listDelayMillis = 700;
            var limited =
                    new McpCapabilityDiscovery(
                            credentials(),
                            new McpTools.DiscoveryLimits(
                                    10, 262144, 1048576, Duration.ofMillis(100)));
            assertThatThrownBy(
                            () ->
                                    limited.discover(
                                            OWNER,
                                            McpContractFixture.release(remote.remote("hotel", "")),
                                            SELECTED))
                    .isInstanceOf(Blocked.class)
                    .hasMessage("mcp_discovery_timeout");
            assertThat(remote.calls.get()).isZero();
        }
    }

    @Test
    void discoveryInsideDatabaseTransactionIsRejectedBeforeNetwork() throws Exception {
        try (var remote = new McpContractFixture()) {
            var credentials = mock(Credentials.class);
            boolean prior = TransactionSynchronizationManager.isActualTransactionActive();
            try {
                TransactionSynchronizationManager.setActualTransactionActive(true);
                assertThatThrownBy(
                                () ->
                                        new McpCapabilityDiscovery(credentials)
                                                .discover(
                                                        OWNER,
                                                        McpContractFixture.release(
                                                                remote.remote("hotel", "")),
                                                        SELECTED))
                        .isInstanceOf(Blocked.class)
                        .hasMessage("mcp_discovery_in_transaction");
            } finally {
                TransactionSynchronizationManager.setActualTransactionActive(prior);
            }
            verifyNoInteractions(credentials);
            assertThat(remote.lists.get()).isZero();
        }
    }

    private static McpCapabilityDiscovery discovery() {
        return new McpCapabilityDiscovery(credentials());
    }

    static Credentials credentials() {
        var credentials = mock(Credentials.class);
        when(credentials.resolve(anyString())).thenReturn("");
        return credentials;
    }
}
