package com.financeclaw.m3;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.tools.*;
import java.util.*;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;

class BusinessContractsTest {
  @Test
  void processBootstrapAndSpringDefaultsProduceTheSameRelease() {
    assertThat(new com.financeclaw.application.ReleaseCatalog().currentHash())
        .isEqualTo(
            new com.financeclaw.application.ReleaseCatalog(new MockEnvironment()).currentHash());
    assertThatThrownBy(
            () ->
                BusinessCatalog.load(
                    new MockEnvironment()
                        .withProperty("financeclaw.business.ziwei.enabled", "true")))
        .hasMessage("Ziwei requires explicit development mode pending M6d acceptance");
  }

  @Test
  void disabledServicesDoNotResolveEndpointsOrSecrets() {
    var env =
        new MockEnvironment()
            .withProperty("financeclaw.business.hotel.enabled", "false")
            .withProperty("financeclaw.business.hotel.url", "${MISSING_URL}")
            .withProperty("financeclaw.business.hotel.credential-ref", "${MISSING_SECRET}")
            .withProperty("financeclaw.business.taibu.enabled", "false");
    assertThat(BusinessCatalog.load(env).remotes()).isEmpty();
  }

  @Test
  void endpointsCannotEscapeAllowlistOrDowngradeExternalTls() {
    assertThatThrownBy(
            () ->
                new BusinessCatalog.Remote(
                    "hotel",
                    "https://evil.test/mcp",
                    List.of("trusted.test"),
                    "",
                    true,
                    List.of(),
                    List.of("searchHotels"),
                    1000))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(
            () ->
                new BusinessCatalog.Remote(
                    "hotel",
                    "http://trusted.test/mcp",
                    List.of("trusted.test"),
                    "",
                    true,
                    List.of(),
                    List.of("searchHotels"),
                    1000))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void almanacRequiresExplicitValidDateUsingNativeMcpArguments() {
    TaibuInputs.validate("almanac", Map.of("date", "2026-10-15"));
    for (var input : List.of(Map.of(), Map.of("date", "2026-02-30"), Map.of("day_offset", 0)))
      assertThatThrownBy(() -> TaibuInputs.validate("almanac", (Map) input))
          .hasMessage("taibu_invalid_input");
  }

  static Map<String, Object> birth() {
    return new TreeMap<>(
        Map.of(
            "gender",
            "male",
            "calendarType",
            "solar",
            "birthYear",
            1990,
            "birthMonth",
            1,
            "birthDay",
            15,
            "birthHour",
            12,
            "birthMinute",
            30));
  }

  @Test
  void baziRequiresExplicitBirthMinuteAndCalendarWithNativeNames() {
    TaibuInputs.validate("bazi", birth());
    var missing = birth();
    missing.remove("birthMinute");
    var lunar = birth();
    lunar.put("calendarType", "lunar");
    var leap = birth();
    leap.put("isLeapMonth", true);
    for (var input : List.of(missing, lunar, leap))
      assertThatThrownBy(() -> TaibuInputs.validate("bazi", input))
          .hasMessage("taibu_invalid_input");
    lunar.put("isLeapMonth", false);
    TaibuInputs.validate("bazi", lunar);
  }

  @Test
  void portfolioPreservesDecimalPrecisionAndRejectsDuplicateSymbols() {
    var p =
        Map.of(
            "portfolio_name",
            "test",
            "positions",
            List.of(Map.of("symbol", "aapl", "quantity", "0.00000001", "cost_basis", 0)));
    assertThat(ResumeInputs.canonical(PortfolioReview.normalize((Map) p)))
        .contains("AAPL", "0.00000001");
    assertThatThrownBy(
            () ->
                PortfolioReview.normalize(
                    Map.of(
                        "portfolio_name",
                        "x",
                        "positions",
                        List.of(
                            Map.of("symbol", "AAPL", "quantity", 1, "cost_basis", 0),
                            Map.of("symbol", "aapl", "quantity", 1, "cost_basis", 0)))))
        .hasMessage("duplicate_portfolio_symbol");
    assertThatThrownBy(
            () ->
                PortfolioReview.normalize(
                    Map.of(
                        "portfolio_name",
                        "x",
                        "positions",
                        List.of(
                            Map.of(
                                "symbol", "AAPL", "quantity", "1e2147483647", "cost_basis", 0)))))
        .hasMessage("invalid_portfolio_decimal");
  }

  @Test
  void exposureMatchesPythonHalfEvenAndThresholdRules() {
    var rows =
        List.of(
            Map.<String, Object>of("price", "100.00", "quantity", "1"),
            Map.<String, Object>of("price", "100.00", "quantity", "3"));
    assertThat(PortfolioReview.analyze(rows))
        .containsEntry("total_market_value", "400.00")
        .containsEntry("largest_position_weight", "0.7500")
        .containsEntry("risk_band", "high");
  }

  @Test
  void nativeClasspathRepositoryLoadsProvenanceLicenseAndInstructions() throws Exception {
    try (var repo =
        new io.agentscope.core.skill.repository.ClasspathSkillRepository(BusinessCatalog.SKILLS)) {
      var skill = repo.getSkill("cocktail-from-what-i-have");
      assertThat(skill.getName()).isEqualTo("cocktail-from-what-i-have");
      assertThat(skill.getResources().get("SOURCE.json"))
          .contains("5a0326ce34b44c015fc26b5c28f6118092c806e4");
      assertThat(skill.getResources().get("LICENSE")).contains("MIT");
      assertThat(skill.getSkillContent()).contains("## Framework: Build From The Shelf");
      assertThat(BusinessCatalog.skillHash()).hasSize(64);
    }
  }
}
