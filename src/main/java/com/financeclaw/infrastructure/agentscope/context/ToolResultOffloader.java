package com.financeclaw.infrastructure.agentscope.context;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.util.JsonUtils;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Function;

/**
 * 在下一次模型推理前，将过大的完整工具回执保存为受治理的 JSON 文件并替换为小引用。
 *
 * <p>本地工具、MCP 和原生 Skill 均以 SDK ToolResultBlock 为入口；调用方传入注册时确定的权限，不能采用模型自报的权限。
 * 归档失败直接向上传播，禁止先丢弃正文或用成功引用掩盖失败。暂停及进行中的回执仍由原生恢复机制处理。
 */
public final class ToolResultOffloader {
    public static final int DEFAULT_THRESHOLD_BYTES = 8192;
    public static final int MIN_THRESHOLD_BYTES = 4096;
    private static final int MAX_THRESHOLD_BYTES = 2097152;
    private final ArtifactService artifacts;
    private final int thresholdBytes;

    public ToolResultOffloader(ArtifactService artifacts, int thresholdBytes) {
        this.artifacts = Objects.requireNonNull(artifacts);
        if (thresholdBytes < MIN_THRESHOLD_BYTES || thresholdBytes > MAX_THRESHOLD_BYTES) {
            throw new IllegalArgumentException(
                    "Tool result threshold must be between 4096 and 2097152 bytes");
        }
        this.thresholdBytes = thresholdBytes;
    }

    /** 保留 SDK 的 callId、工具名及结束状态；原始正文和 metadata 均在归档文件内完整保存。 */
    public ToolResultBlock offload(Claim claim, ToolResultBlock result, String requiredScope) {
        Objects.requireNonNull(result);
        if (result.isSuspended() || result.getState() == ToolResultState.RUNNING) {
            return result;
        }
        // 用发布版本自带的 SDK codec 保留多模态块结构，再按本项目稳定 JSON 规则计算完整文件摘要和字节数。
        var payload = PayloadJson.parse(JsonUtils.getJsonCodec().toJson(result));
        var encoded = ArtifactPayload.encode(payload);
        if (encoded.bytes() <= thresholdBytes) {
            return result;
        }
        var artifact = artifacts.archiveToolResult(claim, payload, requiredScope);
        String reference =
                PayloadJson.canonical(
                        Map.of(
                                "archived",
                                true,
                                "description",
                                "完整工具结果已保存为 JSON 文件；按需读取，不要推测未读取的内容。",
                                "artifact_id",
                                artifact.id(),
                                "hash",
                                artifact.hash(),
                                "bytes",
                                artifact.bytes(),
                                "media_type",
                                "application/json; charset=utf-8",
                                "download_url",
                                "/v1/artifacts/" + artifact.id() + "/download",
                                "read_range",
                                Map.of(
                                        "tool",
                                        "read_artifact_range",
                                        "artifact_id",
                                        artifact.id(),
                                        "start",
                                        0,
                                        "max_bytes",
                                        512,
                                        "instructions",
                                        "start 是 UTF-8 字节偏移；后续使用 nextStart，text 拼接后为完整归档 JSON。")));
        // 不把不受限的原 metadata 复制回模型消息；其完整内容已与正文一起归档，暂停控制字段不经过本分支。
        return new ToolResultBlock(
                result.getId(),
                result.getName(),
                List.of(TextBlock.builder().text(reference).build()),
                Map.of("financeclaw_artifact", artifact.id()),
                result.getState());
    }

    /**
     * 对有效工作上下文进行不可变转换，可在恢复旧检查点后兜底检查原生 Skill 的输出。 调用方只有在本方法完整成功后才替换 AgentState.context，并在执行栅栏内保存该状态。
     */
    public List<Msg> offloadMessages(
            Claim claim, List<Msg> messages, Function<String, String> requiredScope) {
        Objects.requireNonNull(requiredScope);
        var replaced = new ArrayList<Msg>(messages.size());
        for (var message : messages) {
            var blocks = new ArrayList<ContentBlock>(message.getContent().size());
            boolean changed = false;
            for (var block : message.getContent()) {
                var next = block;
                if (block instanceof ToolResultBlock result) {
                    next = offload(claim, result, requiredScope.apply(result.getName()));
                }
                blocks.add(next);
                changed |= next != block;
            }
            replaced.add(changed ? message.withContent(blocks) : message);
        }
        return List.copyOf(replaced);
    }

    /** 将过多的历史产物引用保存为有界索引文件，摘要只保留这个索引引用。 索引只包含引用文本，可以继续引用旧索引；读取目标正文时仍逐个验证目标自身权限，索引不会授予目标访问权。 */
    public List<String> archiveReferences(
            Claim claim, List<String> references, String requiredScope) {
        var copied = List.copyOf(references);
        if (copied.isEmpty()
                || copied.stream().anyMatch(reference -> !reference.startsWith("artifact_ref:"))) {
            throw new IllegalArgumentException(
                    "Artifact index entries must be artifact references");
        }
        var artifact =
                artifacts.archiveToolResult(claim, Map.of("artifact_refs", copied), requiredScope);
        return List.of(
                "artifact_ref:"
                        + PayloadJson.canonical(
                                Map.of(
                                        "artifact_id",
                                        artifact.id(),
                                        "hash",
                                        artifact.hash(),
                                        "bytes",
                                        artifact.bytes())));
    }
}
