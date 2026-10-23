package com.financeclaw.domain.release;

import com.financeclaw.domain.Governance.Blocked;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** 紫微模块的固定策略：版本、输入范围及输出预算与发布身份绑定，计算时不读取可变全局缺省值。 */
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
    private static final int MAX_SEGMENTS = 32;
    private static final int MIN_OUTPUT_BYTES = 1_024;
    private static final int MAX_PROJECTION_BYTES = 14_000;
    private static final int MAX_ARTIFACT_BYTES = 2_097_152;
    private static final String SUPPORTED_TZDB = "2026b";

    public static final String CHART_ENGINE = "financeclaw-java/x-iztro-fixed-0.4.0/1";
    public static final String CALENDAR_RULES = "native-lunar-1.7.7/1";
    public static final String CALENDAR_DEPENDENCY = "cn.6tail:lunar:1.7.7";
    public static final String INPUT_SCHEMA_HASH =
            "d1995d5fd1ee7af28111d04a4e818696f023679e43681180ce92d530817d2ef4";

    public static final String CONVENTION = "financeclaw-ziwei-java-civil@1.0.0";

    public ZiweiPolicy {
        if (!CONVENTION.equals(convention)
                || !CHART_ENGINE.equals(engine)
                || !CALENDAR_RULES.equals(calendar)
                || !SUPPORTED_TZDB.equals(tzdb)
                || !INPUT_SCHEMA_HASH.equals(schemaHash)
                || keyVersion == null
                || !keyVersion.matches("[A-Za-z0-9._-]{1,64}")
                || keyRef == null
                || !keyRef.matches("[A-Z][A-Z0-9_]{0,100}")
                || queryTimezone == null
                || !java.time.ZoneId.getAvailableZoneIds().contains(queryTimezone)
                || maxSegments < 1
                || maxSegments > MAX_SEGMENTS
                || projectionBytes < MIN_OUTPUT_BYTES
                || projectionBytes > MAX_PROJECTION_BYTES
                || artifactBytes < MIN_OUTPUT_BYTES
                || artifactBytes > MAX_ARTIFACT_BYTES) {
            throw new IllegalArgumentException("Invalid fixed Ziwei policy");
        }
    }

    public static ZiweiPolicy development(String keyRef, String keyVersion, String timezone) {
        return new ZiweiPolicy(
                CONVENTION,
                CHART_ENGINE,
                CALENDAR_RULES,
                SUPPORTED_TZDB,
                INPUT_SCHEMA_HASH,
                keyVersion,
                keyRef,
                timezone,
                MAX_SEGMENTS,
                MAX_PROJECTION_BYTES,
                MAX_ARTIFACT_BYTES);
    }

    public void verifyRuntime() {
        if (!tzdb.equals(java.time.zone.ZoneRulesProvider.getVersions(queryTimezone).lastKey())) {
            throw new Blocked("ziwei_tzdb_changed");
        }
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
        if (bytes.length < 32) {
            throw new Blocked("ziwei_key_unavailable");
        }
        return bytes;
    }
}
