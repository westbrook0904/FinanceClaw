package com.financeclawagent;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.agent.port.AgentCapabilityResolver;
import com.financeclaw.app.agent.port.AgentRepository;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.bootstrap.MapperConfiguration;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.infrastructure.agent.CatalogAgentCapabilityResolver;
import com.financeclaw.infrastructure.agent.MyBatisAgentRepository;
import com.financeclaw.infrastructure.agent.ResourceAccessService;
import com.financeclaw.infrastructure.agent.ServerCredentialCatalog;
import com.financeclaw.infrastructure.release.EnvironmentCredentials;
import com.financeclaw.infrastructure.release.MyBatisReleaseRepository;
import com.financeclaw.support.PayloadJson;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintStream;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 本机 operator 的一次性 Agent 管理入口。接受提示词和受控能力引用，不把普通调用 token 解释为平台管理权。
 *
 * <p>不扫描业务组件，不启动 HTTP、Worker、Flyway 或环境发布候选。输出不包含 owner、提示词、manifest、凭据或原始异常。
 */
@SpringBootConfiguration
@EnableAutoConfiguration
@Import({
    MapperConfiguration.Agents.class,
    MapperConfiguration.Releases.class,
    MyBatisAgentRepository.class,
    MyBatisReleaseRepository.class,
    EnvironmentCredentials.class,
    CatalogAgentCapabilityResolver.class,
    ServerCredentialCatalog.class,
    ResourceAccessService.class
})
public class AgentAdministrationApplication {
    static final int MAX_FILE_BYTES = 128 * 1024;
    private static final Set<String> SAFE_ERRORS =
            Set.of(
                    "invalid_agent_id",
                    "agent_access_denied",
                    "credential_access_denied",
                    "invalid_agent_draft",
                    "invalid_draft_revision",
                    "agent_read_only",
                    "not_found",
                    "agent_id_conflict",
                    "agent_draft_revision_changed",
                    "agent_draft_unavailable",
                    "agent_not_published",
                    "agent_release_mismatch",
                    "agent_manifest_invalid",
                    "agent_runtime_incompatible",
                    "agent_identity_mismatch",
                    "invalid_agent_composition",
                    "agent_capability_unavailable",
                    "agent_model_unavailable",
                    "agent_plan_invalid",
                    "agent_capability_resolver_unavailable",
                    "agent_capabilities_unavailable",
                    "agent_draft_invalid",
                    "agent_publish_transaction_active",
                    "mcp_contract_changed",
                    "mcp_contract_invalid",
                    "mcp_discovery_limit_exceeded",
                    "mcp_discovery_timeout",
                    "mcp_discovery_in_transaction",
                    "mcp_unavailable",
                    "mcp_tool_unavailable",
                    "mcp_tool_name_collision",
                    "mcp_credentials_unavailable",
                    "mcp_tenant_denied",
                    "release_unavailable",
                    "release_revision_changed",
                    "invalid_release_revision",
                    "invalid_release_history_page");

    @Bean
    ReleaseService releases(ReleaseRepository repository, TransactionTemplate transactions) {
        return ReleaseService.open(repository, transactions);
    }

    @Bean
    AgentService agents(
            AgentRepository repository,
            ReleaseService releases,
            TransactionTemplate transactions,
            AgentCapabilityResolver capabilities,
            com.financeclaw.app.agent.port.AgentAccessGateway access) {
        var service = new AgentService(repository, releases, transactions, capabilities);
        service.configureAgentAccess(access);
        return service;
    }

    public static void main(String[] args) {
        int status = run(args);
        if (status != 0) {
            System.exit(status);
        }
    }

    static int run(String[] args) {
        Arguments arguments;
        try {
            arguments = parse(args);
        } catch (IllegalArgumentException invalid) {
            System.out.println(failure("invalid_agent_admin_arguments").json());
            return 2;
        }
        DraftInput input;
        try {
            input = arguments.file() == null ? null : readDraft(arguments.file());
        } catch (IllegalArgumentException invalid) {
            System.out.println(failure("invalid_agent_definition").json());
            return 2;
        }
        var output = System.out;
        var errors = System.err;
        Outcome outcome;
        try (var quiet = new PrintStream(OutputStream.nullOutputStream())) {
            System.setOut(quiet);
            System.setErr(quiet);
            try {
                outcome = execute(arguments, input);
            } catch (RuntimeException failed) {
                outcome = failure(failed);
            } finally {
                System.setOut(output);
                System.setErr(errors);
            }
        }
        output.println(outcome.json());
        return outcome.status();
    }

    private static Outcome execute(Arguments arguments, DraftInput input) {
        var application = new SpringApplication(AgentAdministrationApplication.class);
        application.setLogStartupInfo(false);
        try (var context =
                application.run(
                        "--spring.main.web-application-type=none",
                        "--spring.flyway.enabled=false",
                        "--spring.sql.init.mode=never",
                        "--spring.main.banner-mode=off",
                        "--logging.config=classpath:privacy-recovery-logback.xml",
                        "--logging.level.root=OFF",
                        "--management.endpoint.health.group.readiness.include=db",
                        "--financeclaw.worker.enabled=false",
                        "--financeclaw.integrations.enabled=false",
                        "--financeclaw.feishu.enabled=false")) {
            return dispatch(arguments, context.getBean(AgentService.class), input);
        }
    }

    static Outcome dispatch(Arguments arguments, AgentService agents, DraftInput input) {
        Owner owner = arguments.owner();
        String agentId = arguments.agentId();
        Object result =
                switch (arguments.command()) {
                    case "create" ->
                            draftResult(
                                    "created",
                                    input.composition() == null
                                            ? agents.create(
                                                    owner,
                                                    agentId,
                                                    input.displayName(),
                                                    input.systemPrompt())
                                            : agents.create(
                                                    owner,
                                                    agentId,
                                                    input.displayName(),
                                                    input.systemPrompt(),
                                                    input.composition()),
                                    input);
                    case "update" ->
                            draftResult(
                                    "updated",
                                    input.composition() == null
                                            ? agents.update(
                                                    owner,
                                                    agentId,
                                                    arguments.expectedRevision(),
                                                    input.displayName(),
                                                    input.systemPrompt())
                                            : agents.update(
                                                    owner,
                                                    agentId,
                                                    arguments.expectedRevision(),
                                                    input.displayName(),
                                                    input.systemPrompt(),
                                                    input.composition()),
                                    input);
                    case "get" -> draftResult("draft", agents.get(owner, agentId), null);
                    case "catalog" ->
                            Map.of(
                                    "result",
                                    "catalog",
                                    "agentId",
                                    agentId,
                                    "catalog",
                                    agents.catalog(owner, agentId));
                    case "publish" -> {
                        var deployment =
                                agents.publish(
                                        owner,
                                        agentId,
                                        arguments.draftRevision(),
                                        arguments.expectedRevision());
                        yield Map.of(
                                "result",
                                "published",
                                "agentId",
                                agentId,
                                "releaseHash",
                                deployment.releaseHash(),
                                "revision",
                                deployment.revision());
                    }
                    case "history" -> {
                        var changes =
                                agents.history(
                                        owner,
                                        agentId,
                                        arguments.afterRevision(),
                                        arguments.limit());
                        var items =
                                changes.stream()
                                        .map(
                                                change -> {
                                                    var item = new LinkedHashMap<String, Object>();
                                                    item.put("revision", change.revision());
                                                    item.put("releaseHash", change.releaseHash());
                                                    item.put("previousHash", change.previousHash());
                                                    return item;
                                                })
                                        .toList();
                        long next =
                                changes.isEmpty()
                                        ? arguments.afterRevision()
                                        : changes.getLast().revision();
                        yield Map.of(
                                "result", "history",
                                "agentId", agentId,
                                "items", items,
                                "nextAfterRevision", next);
                    }
                    case "list" ->
                            Map.of(
                                    "result",
                                    "agents",
                                    "items",
                                    agents.list(owner).stream()
                                            .map(
                                                    agent ->
                                                            Map.of(
                                                                    "agentId", agent.id(),
                                                                    "draftRevision",
                                                                            agent.draftRevision()))
                                            .toList());
                    default -> throw invalidArguments();
                };
        return new Outcome(0, PayloadJson.canonical(result));
    }

    private static Map<String, Object> draftResult(
            String result, AgentDraft draft, DraftInput input) {
        var report = new LinkedHashMap<String, Object>();
        report.put("result", result);
        report.put("agentId", draft.agentId());
        report.put("draftRevision", draft.revision());
        report.put("baseReleaseHash", draft.baseReleaseHash());
        var content = new LinkedHashMap<String, Object>();
        content.put("displayName", draft.displayName());
        content.put("systemPrompt", draft.systemPrompt());
        if (draft.composition() != null) content.put("composition", draft.composition());
        report.put(
                "contentHash",
                sha256(PayloadJson.canonical(content).getBytes(StandardCharsets.UTF_8)));
        if (input != null) {
            report.put("inputSha256", input.inputSha256());
        }
        return report;
    }

    static Arguments parse(String[] args) {
        if (args == null || args.length == 0 || args.length % 2 != 1) {
            throw invalidArguments();
        }
        String command = args[0];
        Set<String> required =
                switch (command) {
                    case "create" -> Set.of("--tenant", "--subject", "--agent-id", "--file");
                    case "update" ->
                            Set.of(
                                    "--tenant",
                                    "--subject",
                                    "--agent-id",
                                    "--file",
                                    "--expected-revision");
                    case "publish" ->
                            Set.of(
                                    "--tenant",
                                    "--subject",
                                    "--agent-id",
                                    "--draft-revision",
                                    "--expected-revision");
                    case "get", "history", "catalog" ->
                            Set.of("--tenant", "--subject", "--agent-id");
                    case "list" -> Set.of("--tenant", "--subject");
                    default -> throw invalidArguments();
                };
        var allowed = new java.util.HashSet<>(required);
        if (command.equals("history")) {
            allowed.addAll(Set.of("--after-revision", "--limit"));
        }
        var options = new HashMap<String, String>();
        for (int index = 1; index < args.length; index += 2) {
            if (!allowed.contains(args[index])
                    || options.putIfAbsent(args[index], args[index + 1]) != null) {
                throw invalidArguments();
            }
        }
        if (!options.keySet().containsAll(required)) {
            throw invalidArguments();
        }
        String tenant = identity(options.get("--tenant"));
        String subject = identity(options.get("--subject"));
        String agentId = options.get("--agent-id");
        if (agentId != null && (!agentId.equals("finance_agent") || command.equals("create"))) {
            if (!UUID.fromString(agentId).toString().equals(agentId)) {
                throw invalidArguments();
            }
        }
        Path file = options.containsKey("--file") ? Path.of(options.get("--file")) : null;
        if (file != null && options.get("--file").isBlank()) {
            throw invalidArguments();
        }
        long expectedRevision =
                options.containsKey("--expected-revision")
                        ? decimal(
                                options.get("--expected-revision"),
                                command.equals("publish") ? 0 : 1,
                                Long.MAX_VALUE)
                        : 0;
        long draftRevision =
                options.containsKey("--draft-revision")
                        ? decimal(options.get("--draft-revision"), 1, Long.MAX_VALUE)
                        : 0;
        long afterRevision =
                decimal(options.getOrDefault("--after-revision", "0"), 0, Long.MAX_VALUE);
        int limit = (int) decimal(options.getOrDefault("--limit", "20"), 1, 100);
        return new Arguments(
                command,
                new Owner(tenant, subject),
                agentId,
                file,
                expectedRevision,
                draftRevision,
                afterRevision,
                limit);
    }

    static DraftInput readDraft(Path path) {
        try {
            if (!Files.isRegularFile(path)) {
                throw new IllegalArgumentException("invalid_agent_definition");
            }
            byte[] bytes;
            try (var stream = Files.newInputStream(path)) {
                bytes = stream.readNBytes(MAX_FILE_BYTES + 1);
            }
            if (bytes.length == 0 || bytes.length > MAX_FILE_BYTES) {
                throw new IllegalArgumentException("invalid_agent_definition");
            }
            String raw = strictUtf8(bytes);
            var reader =
                    new ObjectMapper()
                            .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
                            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
            var json = reader.readTree(raw);
            if (json == null
                    || !json.isObject()
                    || (json.size() != 2 && json.size() != 3)
                    || (json.size() == 3 && !json.has("composition"))
                    || !json.path("displayName").isTextual()
                    || !json.path("systemPrompt").isTextual()) {
                throw new IllegalArgumentException("invalid_agent_definition");
            }
            String name = json.path("displayName").textValue();
            String prompt = json.path("systemPrompt").textValue();
            if (!AgentDraft.validContent(name, prompt)) {
                throw new IllegalArgumentException("invalid_agent_definition");
            }
            AgentComposition composition = null;
            if (json.has("composition")) {
                var selected = json.path("composition");
                if (!selected.isObject()
                        || selected.size() != 2
                        || !selected.path("modelProfile").isTextual()
                        || !selected.path("capabilities").isArray()) {
                    throw new IllegalArgumentException("invalid_agent_definition");
                }
                var ids = new java.util.ArrayList<String>();
                for (var id : selected.path("capabilities")) {
                    if (!id.isTextual())
                        throw new IllegalArgumentException("invalid_agent_definition");
                    ids.add(id.textValue());
                }
                try {
                    composition =
                            new AgentComposition(selected.path("modelProfile").textValue(), ids);
                } catch (Problem invalid) {
                    throw new IllegalArgumentException("invalid_agent_definition");
                }
            }
            return new DraftInput(name, prompt, sha256(bytes), composition);
        } catch (IOException | SecurityException invalid) {
            throw new IllegalArgumentException("invalid_agent_definition");
        }
    }

    private static String strictUtf8(byte[] bytes) throws CharacterCodingException {
        return StandardCharsets.UTF_8
                .newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes))
                .toString();
    }

    private static String identity(String value) {
        if (value == null
                || value.isBlank()
                || value.length() > 512
                || value.codePoints().anyMatch(Character::isISOControl)) {
            throw invalidArguments();
        }
        return value;
    }

    private static long decimal(String value, long minimum, long maximum) {
        if (value == null || !value.matches("[0-9]+")) {
            throw invalidArguments();
        }
        long parsed = Long.parseLong(value);
        if (parsed < minimum || parsed > maximum) {
            throw invalidArguments();
        }
        return parsed;
    }

    private static String sha256(byte[] bytes) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException unavailable) {
            throw new IllegalStateException("digest_unavailable");
        }
    }

    static Outcome failure(RuntimeException failed) {
        if ((failed instanceof Blocked || failed instanceof Problem)
                && failed.getMessage() != null
                && SAFE_ERRORS.contains(failed.getMessage())) {
            return failure(failed.getMessage());
        }
        return failure("agent_admin_failed");
    }

    private static Outcome failure(String code) {
        return new Outcome(2, PayloadJson.canonical(Map.of("result", "blocked", "error", code)));
    }

    private static IllegalArgumentException invalidArguments() {
        return new IllegalArgumentException("invalid_agent_admin_arguments");
    }

    record Arguments(
            String command,
            Owner owner,
            String agentId,
            Path file,
            long expectedRevision,
            long draftRevision,
            long afterRevision,
            int limit) {}

    record DraftInput(
            String displayName,
            String systemPrompt,
            String inputSha256,
            AgentComposition composition) {
        DraftInput(String displayName, String systemPrompt, String inputSha256) {
            this(displayName, systemPrompt, inputSha256, null);
        }
    }

    record Outcome(int status, String json) {}
}
