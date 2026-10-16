package com.financeclaw.infrastructure.market;

import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.application.ReleaseCatalog;
import org.springframework.stereotype.Component;

@Component
public class CatalogMarketData implements MarketCatalog {
  private final ReleaseCatalog releases;

  public CatalogMarketData(ReleaseCatalog releases) {
    this.releases = releases;
  }

  public String snapshots(String releaseHash) {
    return releases.require(releaseHash).business().marketJson();
  }
}
