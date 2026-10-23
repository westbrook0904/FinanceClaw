package com.financeclaw.infrastructure.market;

import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.app.release.ReleaseService;
import org.springframework.stereotype.Component;

/**
 * 实现 MarketCatalog，从指定发布中读取固定市场快照 JSON。 只返回该 releaseHash
 * 绑定的数据，不调用即时行情供应商或系统时钟；新发布不能悄悄替换执行中任务的数据来源。数据业务校验由 MarketDataService 完成。
 */
@Component
public class CatalogMarketData implements MarketCatalog {
    private final ReleaseService releases;

    public CatalogMarketData(ReleaseService releases) {
        this.releases = releases;
    }

    @Override
    public String snapshots(String releaseHash) {
        return releases.require(releaseHash).business().marketJson();
    }
}
