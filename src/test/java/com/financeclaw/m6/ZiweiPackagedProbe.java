package com.financeclaw.m6;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.ziwei.*;
import java.nio.file.*;
import java.time.LocalDate;
import java.util.Locale;

/** Test-only entry point copied alone into a small launcher jar; it has no expected chart data. */
public final class ZiweiPackagedProbe {
  public static void main(String[] args) throws Exception {
    var json = new ObjectMapper();
    var output = json.createArrayNode();
    for (var row : json.readTree(Files.readString(Path.of(args[0])))) {
      var input = row.get("input");
      var date = input.get("date");
      var solar =
          ZiweiCalendar.solarDate(
              ZiweiCalendar.Calendar.valueOf(
                  input.get("calendar").asText().toUpperCase(Locale.ROOT)),
              date.get(0).asInt(),
              date.get(1).asInt(),
              date.get(2).asInt(),
              input.get("is_leap_month").asBoolean());
      var birth =
          ZiweiChartEngine.Birth.of(
              solar, input.get("shichen").asText(), input.get("sex_for_chart").asText());
      var result = output.addObject();
      result.set("id", row.get("id"));
      result.set("input", input);
      result.put("solar_date", solar.toString());
      result.set("natal", json.valueToTree(ZiweiChartEngine.natal(birth)));
      result.set(
          "horoscope",
          json.valueToTree(
              ZiweiChartEngine.horoscope(birth, LocalDate.parse(input.get("target").asText()))));
    }
    Files.writeString(Path.of(args[1]), json.writeValueAsString(output));
  }
}
