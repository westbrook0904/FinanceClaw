package com.financeclaw.domain.release;

/** 持久发布指针：revision 用于显式切换时的 CAS，任务只绑定解析后的 releaseHash。 */
public record Deployment(String name, String releaseHash, long revision) {}
