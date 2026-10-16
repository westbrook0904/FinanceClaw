package com.financeclaw.domain.release;

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
  public static final String CHART_ENGINE = "financeclaw-java/x-iztro-fixed-0.4.0/1";
  public static final String CALENDAR_RULES = "x-iztro-0.4.0-calendar-compat/1";
  public static final String CALENDAR_DEPENDENCY = "cn.6tail:lunar:1.7.7";
  public static final String INPUT_SCHEMA_HASH =
      "d1995d5fd1ee7af28111d04a4e818696f023679e43681180ce92d530817d2ef4";

  public static final String CONVENTION = "financeclaw-ziwei-java-civil@1.0.0";

  public ZiweiPolicy {
    if (!CONVENTION.equals(convention)
        || !CHART_ENGINE.equals(engine)
        || !CALENDAR_RULES.equals(calendar)
        || !"2026b".equals(tzdb)
        || !INPUT_SCHEMA_HASH.equals(schemaHash)
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
        CHART_ENGINE,
        CALENDAR_RULES,
        "2026b",
        INPUT_SCHEMA_HASH,
        keyVersion,
        keyRef,
        timezone,
        32,
        14000,
        2097152);
  }

  public void verifyRuntime() {
    if (!tzdb.equals(java.time.zone.ZoneRulesProvider.getVersions(queryTimezone).lastKey()))
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
        CALENDAR_DEPENDENCY,
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
