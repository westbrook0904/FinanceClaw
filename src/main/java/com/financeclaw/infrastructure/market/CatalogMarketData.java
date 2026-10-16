package com.financeclaw.infrastructure.market;

import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.app.release.ReleaseService;
import org.springframework.stereotype.Component;

@Component
public class CatalogMarketData implements MarketCatalog {
  private final ReleaseService releases;

  public CatalogMarketData(ReleaseService releases) {
    this.releases = releases;
  }

  public String snapshots(String releaseHash) {
    return releases.require(releaseHash).business().marketJson();
  }
}
