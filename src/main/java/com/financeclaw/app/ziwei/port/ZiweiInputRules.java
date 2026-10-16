package com.financeclaw.app.ziwei.port;

import com.financeclaw.ziwei.*;
import java.util.Map;

public interface ZiweiInputRules {
  ZiweiInputs.Review inspect(String tool, Map<String, Object> input, ZiweiDates.QueryClock clock);

  Map<String, Object> corrected(String tool, Map<String, Object> input, Object answer);

  Map<String, Object> answerSchema(String tool);
}
