package com.financeclaw.domain;

/** 确定性能力步骤的持久结果：输入摘要负责发现键复用冲突，结果保留首次成功提交的完整载荷。 */
public record StepResult(String inputHash, String result) {}
