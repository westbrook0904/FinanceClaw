package com.financeclaw.architecture;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.financeclaw.api.*;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.task.*;
import com.financeclaw.bootstrap.HttpRequestConfiguration;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.AgentDefinition;
import java.util.List;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.json.JsonCompareMode;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

/** 调用者只能选择目标，不能通过普通 HTTP 传入 owner、发布正文或管理操作。 */
class AgentHttpContractTest {
    private final Owner owner = new Owner("agent-http", "caller");
    private final String id = "6f9bac2e-e9d6-46cb-8819-0763a63cf029";
    private AgentService agents;
    private SubmitTaskService tasks;
    private MockMvc http;

    @BeforeEach
    void setup() {
        agents = mock(AgentService.class);
        tasks = mock(SubmitTaskService.class);
        var json =
                new ObjectMapper().setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
        var converter = new MappingJackson2HttpMessageConverter(json);
        new HttpRequestConfiguration().extendMessageConverters(List.of(converter));
        http =
                MockMvcBuilders.standaloneSetup(
                                new AgentController(agents),
                                new TaskController(
                                        tasks,
                                        mock(TaskQueryService.class),
                                        mock(CancelTaskService.class),
                                        mock(TaskEvents.class)))
                        .setControllerAdvice(new ApiErrors())
                        .setMessageConverters(converter)
                        .addFilters(
                                new BearerIdentity(
                                        "agent-http-token", owner.tenant(), owner.subject()))
                        .build();
    }

    @Test
    void directoryUsesAuthenticatedOwnerAndDoesNotExposeOwnerOrPrompt() throws Exception {
        when(agents.list(owner))
                .thenReturn(
                        List.of(
                                new AgentDefinition("finance_agent", null, null, 0),
                                new AgentDefinition(id, owner.tenant(), owner.subject(), 2)));
        http.perform(get("/v1/agents").header("Authorization", "Bearer agent-http-token"))
                .andExpect(status().isOk())
                .andExpect(
                        content()
                                .json(
                                        """
                        [{"agent_id":"finance_agent","draft_revision":0,"builtin":true},
                         {"agent_id":"6f9bac2e-e9d6-46cb-8819-0763a63cf029","draft_revision":2,"builtin":false}]
                        """,
                                        JsonCompareMode.STRICT));
        verify(agents).list(owner);
    }

    @Test
    void creationSelectsOnlyAgentIdAndEmptyRequestPreservesLegacyEntry() throws Exception {
        when(tasks.createConversation(owner, id)).thenReturn("custom");
        when(tasks.createConversation(owner)).thenReturn("legacy");
        http.perform(
                        post("/v1/conversations")
                                .header("Authorization", "Bearer agent-http-token")
                                .contentType("application/json")
                                .content("{\"agent_id\":\"" + id + "\"}"))
                .andExpect(status().isCreated())
                .andExpect(
                        content().json("{\"conversation_id\":\"custom\"}", JsonCompareMode.STRICT));
        http.perform(
                        post("/v1/conversations")
                                .header("Authorization", "Bearer agent-http-token")
                                .contentType("application/json")
                                .content("{}"))
                .andExpect(status().isCreated())
                .andExpect(
                        content().json("{\"conversation_id\":\"legacy\"}", JsonCompareMode.STRICT));
        verify(tasks).createConversation(owner, id);
        verify(tasks).createConversation(owner);
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "{\"agent_id\":null}",
                "{\"agent_id\":42}",
                "{\"agent_id\":\" \"}",
                "{\"agent_id\":\"x\",\"tenant\":\"other\"}",
                "{\"release_hash\":\"known\"}",
                "{\"agent_id\":\"x\",\"system_prompt\":\"override\"}",
                "{\"agent_id\":\"x\",\"admin\":true}"
            })
    void targetRequestRejectsManagementAndIdentityOverrides(String body) throws Exception {
        http.perform(
                        post("/v1/conversations")
                                .header("Authorization", "Bearer agent-http-token")
                                .contentType("application/json")
                                .content(body))
                .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(tasks, agents);
    }

    @Test
    void ordinaryBearerCannotManageAndUnauthorizedCallsCannotList() throws Exception {
        http.perform(get("/v1/agents")).andExpect(status().isUnauthorized());
        http.perform(
                        post("/v1/agents")
                                .header("Authorization", "Bearer agent-http-token")
                                .contentType("application/json")
                                .content("{}"))
                .andExpect(status().isForbidden());
        verifyNoInteractions(agents, tasks);
    }

    @Test
    void inaccessibleTargetIsNotFoundWithoutDisclosingItsOwner() throws Exception {
        when(tasks.createConversation(owner, id)).thenThrow(new Problem(404, "not_found"));
        http.perform(
                        post("/v1/conversations")
                                .header("Authorization", "Bearer agent-http-token")
                                .contentType("application/json")
                                .content("{\"agent_id\":\"" + id + "\"}"))
                .andExpect(status().isNotFound())
                .andExpect(content().json("{\"error\":\"not_found\"}", JsonCompareMode.STRICT));
    }
}
