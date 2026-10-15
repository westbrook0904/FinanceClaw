package com.financeclaw.ziwei;

import com.financeclaw.domain.Governance.Blocked;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** Immutable release policy. Secret material is resolved separately and never serialized. */
public record ZiweiPolicy(
    String convention,
    String engine,
    String calendar,
    String tzdb,
    String schemaHash,
    String keyVersion,
    String keyRef,
    String queryTimezone,
    int maxSegments,
    int projectionBytes,
    int artifactBytes) {
  public static final String CONVENTION = "financeclaw-ziwei-java-civil@1.0.0";

  public ZiweiPolicy {
    if (!CONVENTION.equals(convention)
        || !ZiweiChartEngine.ENGINE.equals(engine)
        || !ZiweiCalendar.RULES.equals(calendar)
        || !"2026b".equals(tzdb)
        || !ZiweiInputs.schemaHash().equals(schemaHash)
        || keyVersion == null
        || !keyVersion.matches("[A-Za-z0-9._-]{1,64}")
        || keyRef == null
        || !keyRef.matches("[A-Z][A-Z0-9_]{0,100}")
        || queryTimezone == null
        || !java.time.ZoneId.getAvailableZoneIds().contains(queryTimezone)
        || maxSegments < 1
        || maxSegments > 32
        || projectionBytes < 1024
        || projectionBytes > 14000
        || artifactBytes < 1024
        || artifactBytes > 2097152)
      throw new IllegalArgumentException("Invalid fixed Ziwei policy");
  }

  public static ZiweiPolicy development(String keyRef, String keyVersion, String timezone) {
    return new ZiweiPolicy(
        CONVENTION,
        ZiweiChartEngine.ENGINE,
        ZiweiCalendar.RULES,
        "2026b",
        ZiweiInputs.schemaHash(),
        keyVersion,
        keyRef,
        timezone,
        32,
        14000,
        2097152);
  }

  public void verifyRuntime() {
    if (!tzdb.equals(ZiweiDates.tzdbVersion(queryTimezone)))
      throw new Blocked("ziwei_tzdb_changed");
  }

  public Map<String, Object> rules() {
    return Map.of(
        "ref",
        convention,
        "engine",
        engine,
        "calendar",
        calendar,
        "calendar_dependency",
        ZiweiCalendar.ENGINE,
        "tzdb",
        tzdb,
        "time_basis",
        "civil",
        "rules",
        "normal-year-month-age/forward-late-zi/default/heaven/fix-leap/zh-CN");
  }

  public static byte[] key(String secret) {
    byte[] bytes = secret == null ? new byte[0] : secret.getBytes(StandardCharsets.UTF_8);
    if (bytes.length < 32) throw new Blocked("ziwei_key_unavailable");
    return bytes;
  }
}
