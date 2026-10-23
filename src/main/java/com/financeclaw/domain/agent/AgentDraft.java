package com.financeclaw.domain.agent;

import com.financeclaw.domain.Task.Problem;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

/** 不可变草稿修订；只开放名称和提示词，基础能力发布在创建时固定。 */
public record AgentDraft(
        String agentId,
        long revision,
        String displayName,
        String systemPrompt,
        String baseReleaseHash) {
    public static final String BUILTIN_AGENT = "finance_agent";
    public static final String BUILTIN_PROMPT = "Use only the tools authorized for this task.";
    public static final int MAX_NAME_CHARS = 200;
    public static final int MAX_PROMPT_BYTES = 16 * 1024;

    public static boolean isCustomId(String id) {
        if (id == null) {
            return false;
        }
        try {
            return UUID.fromString(id).toString().equals(id);
        } catch (IllegalArgumentException invalid) {
            return false;
        }
    }

    public static void requireCustomId(String id) {
        if (BUILTIN_AGENT.equals(id)) {
            throw new Problem(409, "agent_read_only");
        }
        if (!isCustomId(id)) {
            throw new Problem(422, "invalid_agent_id");
        }
    }

    public static boolean validContent(String name, String prompt) {
        return name != null
                && !name.isBlank()
                && name.length() <= MAX_NAME_CHARS
                && isPersistableText(name)
                && prompt != null
                && !prompt.isBlank()
                && isPersistableText(prompt)
                && prompt.getBytes(StandardCharsets.UTF_8).length <= MAX_PROMPT_BYTES;
    }

    private static boolean isPersistableText(String value) {
        // PostgreSQL text 不支持 NUL；拒绝孤立 surrogate，避免 UTF-8 编码替换后正文与摘要失真。
        return value.indexOf('\0') < 0 && StandardCharsets.UTF_8.newEncoder().canEncode(value);
    }

    public static void requireContent(String name, String prompt) {
        if (!validContent(name, prompt)) {
            throw new Problem(422, "invalid_agent_draft");
        }
    }
}
