package com.financeclaw.p2;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.task.*;
import java.util.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 私有目标仍经过原受理事务，Agent 身份和发布共同固定，草稿从不直接进入执行。 */
@SpringBootTest(
        classes = FinanceClawApplication.class,
        properties = {"financeclaw.worker.enabled=false", "financeclaw.integrations.enabled=false"})
class AgentAdmissionIT {
    static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>("postgres:16.10-alpine");

    static {
        PG.start();
    }

    @DynamicPropertySource
    static void database(DynamicPropertyRegistry properties) {
        properties.add("spring.datasource.url", PG::getJdbcUrl);
        properties.add("spring.datasource.username", PG::getUsername);
        properties.add("spring.datasource.password", PG::getPassword);
    }

    @Autowired AgentService agents;
    @Autowired ReleaseService releases;
    @Autowired SubmitTaskService tasks;
    @Autowired CancelTaskService cancellations;
    @Autowired TaskMapper mapper;
    @Autowired DataSource source;
    @Autowired ExecutionRelease executionRelease;
    @Autowired ExecutionPolicyGateway policies;
    @Autowired ExecutionMemory memory;
    @Autowired TransactionTemplate tx;
    @Autowired TaskEventHub events;
    @Autowired WorkSignals signals;
    private Owner owner;
    private JdbcTemplate sql;

    @BeforeEach
    void setup() {
        owner = new Owner("p2-tenant", UUID.randomUUID().toString());
        sql = new JdbcTemplate(source);
    }

    @Test
    void independentAgentsAndPublishedPromptsStayPinnedAcrossDraftsAndReleases() {
        String first = create("First", "Only answer in the first style.");
        String second = create("Second", "Only answer in the second style.");
        var one = agents.publish(owner, first, 1, 0);
        var two = agents.publish(owner, second, 1, 0);
        String oldConversation = tasks.createConversation(owner, first);
        String otherConversation = tasks.createConversation(owner, second);
        agents.update(owner, first, 1, "Changed", "Use the revised style.");
        String beforePublication = tasks.createConversation(owner, first);
        var next = agents.publish(owner, first, 2, 1);
        String newConversation = tasks.createConversation(owner, first);

        var old = submit(oldConversation, "old");
        var draftOnly = submit(beforePublication, "draft-only");
        var fresh = submit(newConversation, "new");
        var other = submit(otherConversation, "other");
        assertThat(old.releaseHash()).isEqualTo(one.releaseHash());
        assertThat(draftOnly.releaseHash()).isEqualTo(one.releaseHash());
        assertThat(fresh.releaseHash()).isEqualTo(next.releaseHash());
        assertThat(other.releaseHash()).isEqualTo(two.releaseHash());
        assertThat(old.agentId()).isEqualTo(first);
        assertThat(other.agentId()).isEqualTo(second);
        assertThat(releases.resolve(old.releaseHash()).systemPrompt()).contains("first style");
        assertThat(releases.resolve(fresh.releaseHash()).systemPrompt()).contains("revised style");
        assertThat(releases.resolve(other.releaseHash()).systemPrompt()).contains("second style");
        assertThat(agents.history(owner, second, 0, 100)).hasSize(1);
        assertThat(agents.history(owner, first, 0, 100)).hasSize(2);
        cancellations.cancel(owner, oldConversation, old.id(), "cancel-old");
        assertThat(submit(oldConversation, "later").releaseHash()).isEqualTo(one.releaseHash());
        assertThat(tasks.submit(owner, oldConversation, "old", "hello").turnId())
                .isEqualTo(old.id());
        assertThat(
                        sql.queryForMap(
                                "SELECT agent_id,release_hash FROM business.conversations WHERE id=?",
                                oldConversation))
                .containsEntry("agent_id", first)
                .containsEntry("release_hash", one.releaseHash());
    }

    @Test
    void targetAuthorizationAndPublicationAreRequiredBeforeConversationWrites() {
        String agent = create("Private", "Private prompt.");
        long before = conversationCount();
        assertThatThrownBy(() -> tasks.createConversation(owner, agent))
                .isInstanceOf(Problem.class)
                .hasMessage("agent_not_published");
        agents.publish(owner, agent, 1, 0);
        for (Owner foreign :
                List.of(
                        new Owner(owner.tenant(), "foreign"),
                        new Owner("foreign", owner.subject()))) {
            assertThatThrownBy(() -> tasks.createConversation(foreign, agent))
                    .hasMessage("not_found");
            assertThat(agents.list(foreign)).noneMatch(item -> item.id().equals(agent));
        }
        assertThat(conversationCount()).isEqualTo(before);
    }

    @Test
    void targetBindingAndGrantCreationRollBackTogether() {
        String agent = create("Atomic", "Atomic prompt.");
        agents.publish(owner, agent, 1, 0);
        long before = conversationCount();
        var admission = spy(new MyBatisTaskAdmissionGateway(mapper, events, signals));
        doAnswer(
                        call -> {
                            call.callRealMethod();
                            throw new IllegalStateException("after target binding");
                        })
                .when(admission)
                .bindConversationAgent(anyString(), eq(agent), anyString());
        var failing =
                new SubmitTaskService(admission, policies, executionRelease, memory, tx, agents);
        assertThatThrownBy(() -> failing.createConversation(owner, agent))
                .hasMessage("after target binding");
        assertThat(conversationCount()).isEqualTo(before);
    }

    @Test
    void immutableTargetAndExecutionScopePreventCrossAgentReuse() {
        String agent = create("Scope", "Scope prompt.");
        var published = agents.publish(owner, agent, 1, 0);
        String conversation = tasks.createConversation(owner, agent);
        var turn = submit(conversation, "scoped");
        var command = new Command("command", turn.id(), "running", "attempt", 1, true, null);
        assertThat(new Claim(command, turn).scope().agent()).isEqualTo(agent);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.conversations SET agent_id='finance_agent' WHERE id=?",
                                        conversation))
                .isInstanceOf(DataIntegrityViolationException.class);
        assertThatThrownBy(
                        () ->
                                sql.update(
                                        "UPDATE business.turns SET agent_id='finance_agent' WHERE id=?",
                                        turn.id()))
                .isInstanceOf(DataIntegrityViolationException.class);
        var forged =
                new Turn(
                        turn.id(),
                        conversation,
                        owner.tenant(),
                        "foreign",
                        "k",
                        "h",
                        "m",
                        published.releaseHash(),
                        "running",
                        1,
                        null,
                        null,
                        agent);
        assertThatThrownBy(() -> executionRelease.requireForTurn(forged))
                // Caller and publisher may differ only with a current exact-release share grant.
                .hasMessage("agent_access_denied");
        var wrongAgent =
                new Turn(
                        turn.id(),
                        conversation,
                        owner.tenant(),
                        owner.subject(),
                        "k",
                        "h",
                        "m",
                        published.releaseHash(),
                        "running",
                        1,
                        null,
                        null,
                        "finance_agent");
        assertThatThrownBy(() -> executionRelease.requireForTurn(wrongAgent))
                .hasMessage("agent_identity_mismatch");
        var legacy = submit(tasks.createConversation(owner), "legacy");
        assertThat(legacy.agentId()).isEqualTo("finance_agent");
        assertThat(new Claim(command, legacy).scope().agent()).isEqualTo("finance_agent");
    }

    private String create(String name, String prompt) {
        String id = UUID.randomUUID().toString();
        agents.create(owner, id, name, prompt);
        return id;
    }

    private Turn submit(String conversation, String key) {
        return mapper.getTurnById(tasks.submit(owner, conversation, key, "hello").turnId());
    }

    private long conversationCount() {
        return sql.queryForObject(
                "SELECT count(*) FROM business.conversations WHERE tenant=? AND subject=?",
                Long.class,
                owner.tenant(),
                owner.subject());
    }
}
