package com.financeclaw.domain.release;

import java.util.*;

/** 固定发布版本中的业务配置：远程工具、技能内容摘要、行情快照及紫微策略共同构成执行环境。 */
public record BusinessFeatures(
        List<RemoteEndpoint> remotes, String skillHash, String marketJson, ZiweiPolicy ziwei) {

    public BusinessFeatures {
        remotes = List.copyOf(remotes);
    }
}
