package com.financeclaw.channel;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Feishu.Binding;
import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.util.*;

/**
 * 生成受限的飞书文本，并解析与当前问题绑定的回答。
 *
 * <p>交互令牌绑定会话、任务和问题版本；等待提示不携带工具参数；完成通知只读取该主体已经发布的结果。
 */
public final class FeishuText {
    private static final int INTERACTION_TOKEN_CHARACTERS = 20;
    private static final int MAX_NOTICE_CHARACTERS = 4000;

    private FeishuText() {}

    public static String token(Binding b, Turn t, Interaction i) {
        return ExecutionScope.hash(
                        List.of(
                                b.id(),
                                Long.toString(b.revision()),
                                t.id(),
                                Long.toString(t.revision()),
                                i.id(),
                                Long.toString(i.revision()),
                                i.actionHash()))
                .substring(0, INTERACTION_TOKEN_CHARACTERS);
    }

    public static String render(Binding b, Turn t, List<Interaction> interactions) {
        String text;
        if (t.status().equals("completed")) {
            text = Objects.toString(t.result(), "");
        } else if (t.status().equals("waiting")) {
            var parts = new ArrayList<String>();
            for (var i : interactions) {
                if (!i.live() || !i.status().equals("pending")) {
                    continue;
                }
                if (i.kind().equals("approval")) {
                    String token = token(b, t, i);
                    parts.add(
                            "等待确认："
                                    + i.tool()
                                    + "\n批准请回复 /approve "
                                    + token
                                    + "\n拒绝请回复 /reject "
                                    + token);
                } else {
                    parts.add(
                            i.question()
                                    + "\n"
                                    + (singleTextField(i) != null
                                            ? "请直接回复答案。"
                                            : "请在当前任务的交互入口填写所需信息。"));
                }
            }
            text = String.join("\n", parts);
        } else {
            text =
                    switch (t.status()) {
                        case "cancelled" -> "任务已取消。";
                        case "blocked" -> "任务已暂停，请检查当前任务状态。";
                        default -> "任务未能完成。";
                    };
        }
        return text.length() > MAX_NOTICE_CHARACTERS
                ? text.substring(0, MAX_NOTICE_CHARACTERS) + "\n（完整结果见任务详情）"
                : text;
    }

    private static String singleTextField(Interaction i) {
        Object raw = PayloadJson.parse(i.schema());
        if (!(raw instanceof Map<?, ?> schema)
                || !(schema.get("properties") instanceof Map<?, ?> properties)
                || properties.size() != 1) {
            return null;
        }
        var entry = properties.entrySet().iterator().next();
        return entry.getValue() instanceof Map<?, ?> field && "string".equals(field.get("type"))
                ? (String) entry.getKey()
                : null;
    }

    public static Object answer(Interaction i, String text) {
        String field = singleTextField(i);
        if (field == null) {
            throw new Problem(422, "use_business_interaction");
        }
        return Map.of(field, text);
    }
}
