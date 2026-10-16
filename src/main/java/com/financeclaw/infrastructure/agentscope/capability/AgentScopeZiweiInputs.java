package com.financeclaw.infrastructure.agentscope.capability;

import static com.financeclaw.ziwei.ZiweiValues.map;

import com.financeclaw.app.ziwei.port.ZiweiInputRules;
import com.financeclaw.infrastructure.ziwei.*;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.ziwei.*;
import io.agentscope.core.tool.ToolValidator;
import java.util.*;
import org.springframework.stereotype.Component;

/** AgentScope validates JSON structure; the calculation kernel owns birth and date semantics. */
@Component
public class AgentScopeZiweiInputs implements ZiweiInputRules {
  public ZiweiInputs.Review inspect(
      String tool, Map<String, Object> input, ZiweiDates.QueryClock clock) {
    if (input == null
        || ToolValidator.validateInput(PayloadJson.canonical(input), ZiweiSchemas.schema(tool))
            != null)
      return new ZiweiInputs.Review(
          null, List.of(new ZiweiInputs.Issue("arguments", "ZIWEI_TOOL_INPUT_INVALID")));
    return ZiweiInputs.inspect(tool, input, clock);
  }

  public Map<String, Object> answerSchema(String tool) {
    return ZiweiSchemas.answerSchema(tool);
  }

  public Map<String, Object> corrected(String tool, Map<String, Object> input, Object answer) {
    if (ToolValidator.validateInput(PayloadJson.canonical(answer), ZiweiSchemas.answerSchema(tool))
        != null) throw new IllegalArgumentException("Invalid Ziwei clarification");
    Map<String, Object> patch = map(answer);
    var merged = new TreeMap<>(input);
    Set<String> targets =
        map(map(ZiweiSchemaResource.document().get("targets")).get(tool)).keySet();
    // A new date selector replaces the old selector, not silently combines two forms.
    if (patch.keySet().stream().anyMatch(targets::contains)) {
      boolean sameForm =
          (patch.containsKey("date_range") && input.containsKey("date_range"))
              || ((patch.containsKey("year") || patch.containsKey("month"))
                  && (input.containsKey("year") || input.containsKey("month")));
      if (!sameForm) targets.forEach(merged::remove);
    }
    return merge(merged, patch);
  }

  private static Map<String, Object> merge(
      Map<String, Object> original, Map<String, Object> patch) {
    var result = new TreeMap<>(original);
    // Changing precision replaces the time representation; keep same-kind partial corrections.
    if (patch.containsKey("kind") && !Objects.equals(patch.get("kind"), original.get("kind")))
      return ZiweiValues.immutable(patch);
    patch.forEach(
        (k, v) ->
            result.put(
                k,
                v instanceof Map<?, ?> && original.get(k) instanceof Map<?, ?>
                    ? merge(map(original.get(k)), map(v))
                    : v));
    return Collections.unmodifiableMap(result);
  }
}
