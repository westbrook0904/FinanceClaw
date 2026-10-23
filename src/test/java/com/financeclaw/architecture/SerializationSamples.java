package com.financeclaw.architecture;

import com.financeclaw.app.release.ReleaseIdentity;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.ToolUseBlock;
import java.math.BigDecimal;
import java.util.*;

/** 当前序列化契约的固定输入；预期 JSON 和长度前缀摘要独立保存在测试资源中。 */
public final class SerializationSamples {
    private SerializationSamples() {}

    public static Map<String, String> actual() {
        var result = new TreeMap<String, String>();
        var payload = new LinkedHashMap<String, Object>();
        payload.put("中文", "上海😀\n\"quoted\"\\path");
        payload.put("z", null);
        payload.put("numbers", List.of(1, 1.0, new BigDecimal("1.2300"), Long.MAX_VALUE));
        payload.put("a", Map.of("z", false, "a", List.of("second", "first")));
        String canonical = PayloadJson.canonical(payload);
        result.put("canonical", canonical);
        result.put("payload_digest", PayloadDigest.sha256(canonical));
        result.put("parsed_canonical", PayloadJson.canonical(PayloadJson.parse(canonical)));
        result.put(
                "action_hash",
                ResumeInputs.actionHash(
                        ToolUseBlock.builder()
                                .id("call-中文-1")
                                .name("prepare_portfolio_review")
                                .input(payload)
                                .build()));
        var primary =
                new ModelProfile(
                        "qwen-fixture", "https://model.example/v1", 65536, 131072, 2048, 512, true);
        var features =
                new BusinessFeatures(
                        List.of(
                                new RemoteEndpoint(
                                        "hotel",
                                        "https://hotel.example/mcp",
                                        List.of("hotel.example"),
                                        "FIXTURE_CREDENTIAL",
                                        true,
                                        List.of("tenant-b", "tenant-a"),
                                        List.of("searchHotels", "getHotelDetail"),
                                        5000)),
                        "frozen-skill-hash",
                        "{\"z\":1,\"a\":2}",
                        ZiweiPolicy.development(
                                "FIXTURE_ZIWEI_KEY", "fixture-v1", "Asia/Shanghai"));
        var release =
                new ReleaseDefinition(
                        "serialization-fixture",
                        "finance_agent",
                        List.of("ziwei:read", "artifact:read"),
                        primary,
                        ReleaseDefinition.OFFLINE,
                        12,
                        32,
                        features);
        result.put("release_json", PayloadJson.canonical(release));
        result.put("release_hash", ReleaseIdentity.hash(release));
        var minimal =
                new ReleaseDefinition(
                        "minimal-fixture",
                        "finance_agent",
                        List.of(),
                        ReleaseDefinition.OFFLINE,
                        null,
                        1,
                        1,
                        new BusinessFeatures(List.of(), "fixed", "{}", null));
        result.put("minimal_release_json", PayloadJson.canonical(minimal));
        result.put("minimal_release_hash", ReleaseIdentity.hash(minimal));
        var scope =
                new ExecutionScope(
                        "tenant-中文",
                        "subject",
                        "conversation",
                        "finance_agent",
                        ReleaseIdentity.hash(release));
        result.put("namespace", scope.namespace());
        result.put("user_id", scope.userId());
        result.put("session_id", scope.sessionId());
        result.put("scope_key", scope.key());
        return result;
    }
}
