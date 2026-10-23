package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.support.PayloadJson;
import java.util.Map;
import org.junit.jupiter.api.Test;

class CanonicalSerializationTest {
    @Test
    void canonicalJsonUtf8DigestsAndExecutionScopeMatchTheCurrentContract() throws Exception {
        try (var in = getClass().getResourceAsStream("/architecture/serialization-contract.json")) {
            assertThat(in).isNotNull();
            Map<String, String> expected =
                    new ObjectMapper().readValue(in, new TypeReference<>() {});
            assertThat(SerializationSamples.actual()).containsExactlyInAnyOrderEntriesOf(expected);
        }
    }

    @Test
    void corruptStoredPayloadsReturnSafeDomainErrors() {
        assertThatThrownBy(() -> PayloadJson.parse("{"))
                .isInstanceOf(Blocked.class)
                .hasMessage("invalid_stored_json");
        assertThatThrownBy(() -> PayloadJson.object("[]"))
                .isInstanceOf(Blocked.class)
                .hasMessage("invalid_stored_schema");
        assertThatThrownBy(() -> PayloadJson.scopes("{}"))
                .isInstanceOf(Blocked.class)
                .hasMessage("invalid_stored_scopes");
    }
}
