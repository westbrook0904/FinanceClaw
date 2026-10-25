package com.financeclaw.app.release.port;

import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.domain.agent.ComposedAgentReleaseManifest;
import com.financeclaw.domain.release.ReleaseDefinition;

/**
 * 一次发布目录读取的类型化结果，不代表已经通过执行校验或取得调用权限。
 *
 * <p>持久化适配器依据数据库的 manifest_format 选择唯一类型，不尝试多个格式反序列化。Agent 根清单与它指向的单层基础清单 在同一条查询中读取；服务层仍逐次校验请求
 * hash、外层清单、基础清单以及 v3 执行计划，不把该聚合缓存为跨事务授权。
 */
public sealed interface StoredRelease
        permits StoredRelease.Legacy, StoredRelease.AgentV2, StoredRelease.AgentV3 {
    String hash();

    record Legacy(String hash, ReleaseDefinition definition) implements StoredRelease {}

    /** base 为 null 表示缺失、非 legacy 格式或无法解码，必须由服务层在外层校验后拒绝。 */
    record AgentV2(String hash, AgentReleaseManifest manifest, LegacyBase base)
            implements StoredRelease {}

    /** 保存原始外层清单及基础身份，不能只返回投影而丢失两层摘要的校验依据。 */
    record AgentV3(String hash, ComposedAgentReleaseManifest manifest, LegacyBase base)
            implements StoredRelease {}

    /** 基础清单仅限物理 legacy-v1 格式；hash 仍由服务层与外层引用和正文摘要逐一核对。 */
    record LegacyBase(String hash, ReleaseDefinition definition) {}
}
