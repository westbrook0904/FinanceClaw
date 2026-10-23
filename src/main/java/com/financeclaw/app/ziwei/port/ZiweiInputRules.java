package com.financeclaw.app.ziwei.port;

import com.financeclaw.ziwei.*;
import java.util.Map;

/** 紫微交互校验边界：仅校验当前检查点所请求的缺失字段，不能扩大工具输入或直接执行计算。 */
public interface ZiweiInputRules {
    ZiweiInputs.Review inspect(String tool, Map<String, Object> input, ZiweiDates.QueryClock clock);

    Map<String, Object> corrected(String tool, Map<String, Object> input, Object answer);

    Map<String, Object> answerSchema(String tool);
}
