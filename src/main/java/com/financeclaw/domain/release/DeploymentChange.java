package com.financeclaw.domain.release;

/** 不可变指针变更回执；首次初始化的 previousHash 为空，后续回滚也生成新的 revision。 */
public record DeploymentChange(
        String deployment, long revision, String releaseHash, String previousHash) {}
