package com.financeclaw.architecture;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.financeclaw.api.*;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.agent.port.ResourceAdministration;
import com.financeclaw.bootstrap.HttpRequestConfiguration;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.authorization.CredentialResource;
import com.financeclaw.domain.authorization.ResourceGrant;
import java.util.List;
import org.junit.jupiter.api.*;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class AgentManagementHttpTest {
    private final Owner owner = new Owner("t", "publisher");
    private final String id = "6f9bac2e-e9d6-46cb-8819-0763a63cf029";
    private AgentService agents;
    private ResourceAdministration access;
    private MockMvc http;

    @BeforeEach
    void setup() {
        agents = mock(AgentService.class);
        access = mock(ResourceAdministration.class);
        var json =
                new ObjectMapper().setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
        var converter = new MappingJackson2HttpMessageConverter(json);
        new HttpRequestConfiguration().extendMessageConverters(List.of(converter));
        var directory =
                new TrustedIdentityDirectory(
                        "caller",
                        "t",
                        "reader",
                        """
            [{"token":"manager","tenant":"t","subject":"publisher","roles":["agent_manager","agent_sharer","credential_manager"]},
             {"token":"other-manager","tenant":"t","subject":"other","roles":["agent_manager"]}]
            """);
        http =
                MockMvcBuilders.standaloneSetup(
                                new AgentController(agents, access),
                                new ResourceAccessController(access))
                        .setControllerAdvice(new ApiErrors())
                        .setMessageConverters(converter)
                        .addFilters(new BearerIdentity(directory))
                        .build();
    }

    @Test
    void managerUsesTrustedOwnerAndPassesExplicitDraftCas() throws Exception {
        var draft = new AgentDraft(id, 2, "Name", "Prompt", "a".repeat(64));
        when(agents.update(owner, id, 1, "Name", "Prompt", null)).thenReturn(draft);
        http.perform(
                        put("/v1/agents/" + id)
                                .header("Authorization", "Bearer manager")
                                .contentType("application/json")
                                .content(
                                        """
                {"expected_draft_revision":1,"display_name":"Name","system_prompt":"Prompt"}
                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.revision").value(2));
        verify(agents).update(owner, id, 1, "Name", "Prompt", null);
        http.perform(
                        delete("/v1/agents/" + id)
                                .param("expectedDraftRevision", "2")
                                .header("Authorization", "Bearer manager"))
                .andExpect(status().isNoContent());
        verify(access).archive(owner, id, 2);
    }

    @Test
    void ordinaryTokenCannotReadManagementOrCreateShareAndNoBodyCanChooseOwner() throws Exception {
        http.perform(get("/v1/agents/" + id).header("Authorization", "Bearer caller"))
                .andExpect(status().isForbidden());
        http.perform(
                        put("/v1/agents/" + id + "/shares")
                                .header("Authorization", "Bearer caller")
                                .contentType("application/json")
                                .content("{}"))
                .andExpect(status().isForbidden());
        http.perform(
                        post("/v1/agents")
                                .header("Authorization", "Bearer manager")
                                .contentType("application/json")
                                .content("{\"tenant\":\"victim\",\"subject\":\"x\"}"))
                .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(agents, access);
    }

    @Test
    void managementRoleNeverOverridesResourceOwnershipOrCas() throws Exception {
        when(agents.get(new Owner("t", "other"), id)).thenThrow(new Problem(404, "not_found"));
        http.perform(get("/v1/agents/" + id).header("Authorization", "Bearer other-manager"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.error").value("not_found"));
        http.perform(
                        post("/v1/agents/" + id + "/publish")
                                .header("Authorization", "Bearer manager")
                                .contentType("application/json")
                                .content("{\"expected_draft_revision\":1}"))
                .andExpect(status().isUnprocessableEntity());
        verify(agents, never()).publish(any(), anyString(), anyLong(), anyLong());
    }

    @Test
    void sharedGrantNamesRecipientButNeverReplacesAuthenticatedResourceOwner() throws Exception {
        var grantee = new Owner("other-tenant", "reader");
        var hash = "b".repeat(64);
        when(access.change(owner, "agent", id, grantee, hash, 0, true))
                .thenReturn(
                        new ResourceGrant(
                                "agent",
                                id,
                                "t",
                                "publisher",
                                "other-tenant",
                                "reader",
                                hash,
                                null,
                                1,
                                true));
        http.perform(
                        put("/v1/agents/" + id + "/shares")
                                .header("Authorization", "Bearer manager")
                                .contentType("application/json")
                                .content(
                                        "{\"grantee_tenant\":\"other-tenant\",\"grantee_subject\":\"reader\",\"release_hash\":\""
                                                + hash
                                                + "\",\"expected_revision\":0,\"enabled\":true}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.revision").value(1))
                .andExpect(jsonPath("$.resource_owner").doesNotExist());
        verify(access).change(owner, "agent", id, grantee, hash, 0, true);
    }

    @Test
    void credentialManagementExposesOnlyCatalogIdsAndRejectsRawSecretsAndReferences()
            throws Exception {
        when(access.credentialResources(owner))
                .thenReturn(List.of(new CredentialResource("search", "SECRET_REF", owner)));
        http.perform(get("/v1/credential-resources").header("Authorization", "Bearer manager"))
                .andExpect(status().isOk())
                .andExpect(content().json("[{\"resource_id\":\"search\"}]"))
                .andExpect(jsonPath("$[0].reference").doesNotExist());
        http.perform(
                        put("/v1/credential-resources/search/grants")
                                .header("Authorization", "Bearer manager")
                                .contentType("application/json")
                                .content("{\"reference\":\"SECRET_REF\",\"secret\":\"secret\"}"))
                .andExpect(status().isUnprocessableEntity());
        verify(access, never())
                .change(any(), anyString(), anyString(), any(), any(), anyLong(), anyBoolean());
    }

    @Test
    void grantHistoryRequiresARecipientAndRetainsRoleGateForMissingParameters() throws Exception {
        for (String path :
                List.of(
                        "/v1/agents/" + id + "/shares/history",
                        "/v1/credential-resources/search/grants/history")) {
            http.perform(get(path).header("Authorization", "Bearer manager"))
                    .andExpect(status().isUnprocessableEntity())
                    .andExpect(jsonPath("$.error").value("invalid_resource_grant"));
            http.perform(get(path).header("Authorization", "Bearer caller"))
                    .andExpect(status().isForbidden());
        }
        verifyNoInteractions(agents, access);
    }
}
