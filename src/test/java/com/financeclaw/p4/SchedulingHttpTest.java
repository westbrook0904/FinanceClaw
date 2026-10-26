package com.financeclaw.p4;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.financeclaw.api.*;
import com.financeclaw.app.scheduling.DispatchPolicyService;
import com.financeclaw.app.scheduling.TriggerService;
import com.financeclaw.bootstrap.HttpRequestConfiguration;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.scheduling.ConcurrencyLimit;
import com.financeclaw.domain.scheduling.TriggerRule;
import java.util.List;
import org.junit.jupiter.api.*;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class SchedulingHttpTest {
    private TriggerService triggers;
    private DispatchPolicyService policies;
    private MockMvc http;
    private final Owner owner = new Owner("tenant", "scheduler");
    private final String id = "22a7dcfb-90be-451d-8e62-f24a8a40e6f6";

    @BeforeEach
    void setup() {
        triggers = mock(TriggerService.class);
        policies = mock(DispatchPolicyService.class);
        var converter =
                new MappingJackson2HttpMessageConverter(
                        new ObjectMapper()
                                .setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE));
        new HttpRequestConfiguration().extendMessageConverters(List.of(converter));
        var directory =
                new TrustedIdentityDirectory(
                        "reader",
                        "tenant",
                        "reader",
                        """
            [{"token":"scheduler-token","tenant":"tenant","subject":"scheduler","roles":["scheduler_manager"]},
             {"token":"runtime-token","tenant":"tenant","subject":"scheduler","roles":["runtime_manager"]}]
            """);
        http =
                MockMvcBuilders.standaloneSetup(
                                new ScheduleController(triggers), new DispatchController(policies))
                        .setControllerAdvice(new ApiErrors())
                        .setMessageConverters(converter)
                        .addFilters(new BearerIdentity(directory))
                        .build();
    }

    @Test
    void ordinaryCredentialsCannotManageAndBodyCannotChooseOwner() throws Exception {
        http.perform(get("/v1/schedules").header("Authorization", "Bearer reader"))
                .andExpect(status().isForbidden());
        http.perform(
                        put("/v1/schedules/" + id)
                                .header("Authorization", "Bearer scheduler-token")
                                .contentType("application/json")
                                .content("{\"expected_revision\":0,\"tenant\":\"victim\"}"))
                .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(triggers, policies);
    }

    @Test
    void explicitRevisionAndDefinitionReachServiceUnderAuthenticatedOwner() throws Exception {
        String definition =
                "{\"enabled\":true,\"kind\":\"webhook\",\"misfire\":\"fire_once\",\"overlap\":\"allow\",\"agent_id\":\"finance_agent\",\"version_policy\":\"fixed\",\"message\":\"work\",\"priority\":0}";
        http.perform(
                        put("/v1/schedules/" + id)
                                .header("Authorization", "Bearer scheduler-token")
                                .contentType("application/json")
                                .content(
                                        "{\"expected_revision\":0,\"definition\":"
                                                + definition
                                                + "}"))
                .andExpect(status().isOk());
        verify(triggers)
                .put(
                        owner,
                        id,
                        0,
                        new TriggerRule.Definition(
                                true,
                                "webhook",
                                null,
                                null,
                                "fire_once",
                                "allow",
                                "finance_agent",
                                "fixed",
                                "work",
                                0));
        reset(triggers);
        http.perform(
                        put("/v1/schedules/" + id)
                                .header("Authorization", "Bearer scheduler-token")
                                .contentType("application/json")
                                .content("{\"definition\":" + definition + "}"))
                .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(triggers);
    }

    @Test
    void webhookAcceptsOnlyEmptyBodyAndPassesStableEventKey() throws Exception {
        http.perform(
                        post("/v1/schedules/" + id + "/events")
                                .header("Authorization", "Bearer scheduler-token")
                                .header("Idempotency-Key", "event-one")
                                .contentType("application/json")
                                .content("{\"message\":\"override\"}"))
                .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(triggers);
        http.perform(
                        post("/v1/schedules/" + id + "/events")
                                .header("Authorization", "Bearer scheduler-token")
                                .header("Idempotency-Key", "event-one")
                                .contentType("application/json")
                                .content("{}"))
                .andExpect(status().isOk());
        verify(triggers).webhook(owner, id, "event-one");
    }

    @Test
    void tenantWideLimitRequiresSeparateRuntimeRoleAndExplicitCas() throws Exception {
        String body = "{\"revision\":0,\"maximum_running\":10}";
        http.perform(
                        put("/v1/dispatch/tenant-policy")
                                .header("Authorization", "Bearer scheduler-token")
                                .contentType("application/json")
                                .content(body))
                .andExpect(status().isForbidden());
        verifyNoInteractions(policies);
        when(policies.updateTenant(owner, new ConcurrencyLimit(0, 10)))
                .thenReturn(new ConcurrencyLimit(1, 10));
        http.perform(
                        put("/v1/dispatch/tenant-policy")
                                .header("Authorization", "Bearer runtime-token")
                                .contentType("application/json")
                                .content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.revision").value(1));
        verify(policies).updateTenant(owner, new ConcurrencyLimit(0, 10));
    }

    @Test
    void sharedAgentTotalLimitRequiresRuntimeRoleAndCannotSelectAnotherTenant() throws Exception {
        String path = "/v1/dispatch/agents/finance_agent/policy";
        String body = "{\"revision\":0,\"maximum_running\":3}";
        http.perform(
                        put(path)
                                .header("Authorization", "Bearer scheduler-token")
                                .contentType("application/json")
                                .content(body))
                .andExpect(status().isForbidden());
        verifyNoInteractions(policies);
        when(policies.updateAgent(owner, "finance_agent", new ConcurrencyLimit(0, 3)))
                .thenReturn(new ConcurrencyLimit(1, 3));
        http.perform(
                        put(path)
                                .header("Authorization", "Bearer runtime-token")
                                .contentType("application/json")
                                .content(body))
                .andExpect(status().isOk());
        verify(policies).updateAgent(owner, "finance_agent", new ConcurrencyLimit(0, 3));
        reset(policies);
        http.perform(
                        put(path)
                                .header("Authorization", "Bearer runtime-token")
                                .contentType("application/json")
                                .content(
                                        "{\"revision\":1,\"maximum_running\":3,\"tenant\":\"another\"}"))
                .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(policies);
    }
}
