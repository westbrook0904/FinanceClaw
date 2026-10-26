package com.financeclaw.app.execution.port;

import com.financeclaw.domain.execution.ChildInvocation;
import java.util.List;

/**
 * 固定子调用的存储边界；不提供子队列或独立调度生命周期。
 *
 * <p>调用方持根 Command fence、隐私及授权锁。预算只在 pending 且输入摘要相同时原子预留； complete 与已有 result 步骤提交在同一事务内。
 */
public interface ChildInvocationGateway {
    ChildInvocation get(String root, String call);

    int insertIfAbsent(String root, String call, String hash);

    int insertPrivateIfAbsent(
            String root, String call, String hash, String targetAgent, String targetRelease);

    /** 固定市场 profile，存储适配器写死 market:read，不能传任意 scope。 */
    int insertPrivateMarketIfAbsent(
            String root, String call, String hash, String targetAgent, String targetRelease);

    int reserveModel(String root, String call, String hash, int limit);

    int reserveTool(String root, String call, String hash);

    /** 私有市场叶子仅一次工具读取，独立于旧研究十次上限。 */
    int reservePrivateMarketTool(String root, String call, String hash);

    int complete(String root, String call, String hash);

    boolean hasSteps(String root, String namespace);

    List<String> snapshots(String root, String namespace);

    default int insertPrivateReadOnlyIfAbsent(
            String root,
            String call,
            String hash,
            String targetAgent,
            String targetRelease,
            List<String> scopes,
            String profile) {
        throw new UnsupportedOperationException("Read-only leaf storage unavailable");
    }

    default int reservePrivateReadOnlyTool(String root, String call, String hash, int limit) {
        throw new UnsupportedOperationException("Read-only leaf storage unavailable");
    }
}
