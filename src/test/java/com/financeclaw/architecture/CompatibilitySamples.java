package com.financeclaw.architecture;

import com.financeclaw.application.ReleaseCatalog;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.tools.BusinessCatalog;
import com.financeclaw.ziwei.ZiweiPolicy;
import io.agentscope.core.message.ToolUseBlock;
import java.math.BigDecimal;
import java.util.*;

/** Fixed inputs captured before R1; expected bytes are stored separately in resources. */
public final class CompatibilitySamples {
  private CompatibilitySamples() {}

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
        new ReleaseCatalog.ModelProfile(
            "qwen-fixture", "https://model.example/v1", 65536, 131072, 2048, 512, true);
    var features =
        new BusinessCatalog.Features(
            List.of(
                new BusinessCatalog.Remote(
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
            ZiweiPolicy.development("FIXTURE_ZIWEI_KEY", "fixture-v1", "Asia/Shanghai"));
    var release =
        new ReleaseCatalog.Release(
            "compat-v1",
            "finance_agent",
            List.of("ziwei:read", "artifact:read"),
            primary,
            ReleaseCatalog.OFFLINE,
            12,
            32,
            features);
    result.put("release_json", PayloadJson.canonical(release));
    result.put("release_hash", release.hash());
    var minimal =
        new ReleaseCatalog.Release(
            "compat-minimal",
            "finance_agent",
            List.of(),
            ReleaseCatalog.OFFLINE,
            null,
            1,
            1,
            new BusinessCatalog.Features(List.of(), "fixed", "{}"));
    result.put("minimal_release_json", PayloadJson.canonical(minimal));
    result.put("minimal_release_hash", minimal.hash());
    var scope =
        new ExecutionScope("tenant-中文", "subject", "conversation", "finance_agent", release.hash());
    result.put("namespace", scope.namespace());
    result.put("user_id", scope.userId());
    result.put("session_id", scope.sessionId());
    result.put("scope_key", scope.key());
    return result;
  }
}
