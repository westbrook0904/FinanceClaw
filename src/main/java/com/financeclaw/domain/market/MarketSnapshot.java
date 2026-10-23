package com.financeclaw.domain.market;

import com.financeclaw.domain.Governance.Blocked;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.OffsetDateTime;

/** 固定时点行情：价格、币种、来源和采样时间一起参与验证，避免把过期或不同市场数据混用。 */
public record MarketSnapshot(
        String symbol, BigDecimal price, String currency, String provider, String asOf) {
    public MarketSnapshot {
        try {
            if (price.signum() <= 0
                    || (long) price.precision() + Math.max(0L, -(long) price.scale()) > 24
                    || price.scale() > 8
                    || provider == null
                    || provider.isBlank()
                    || provider.length() > 128
                    || currency == null
                    || !currency.matches("[A-Z]{3}")) {
                throw new IllegalArgumentException();
            }
            OffsetDateTime.parse(asOf);
        } catch (RuntimeException e) {
            throw new Blocked("invalid_market_snapshot");
        }
    }

    public Instant time() {
        return OffsetDateTime.parse(asOf).toInstant();
    }
}
