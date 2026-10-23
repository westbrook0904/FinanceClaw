package com.financeclaw.app.market.port;

/** 固定行情目录读取边界：按任务绑定的发布版本返回快照，不能从当前全局配置替换历史输入。 */
public interface MarketCatalog {
    String snapshots(String releaseHash);
}
