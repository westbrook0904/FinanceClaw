package com.financeclaw.app.portfolio.port;

import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;

/** 组合复核结果发布边界：产物与报告仍执行当前执行租约、授权和隐私检查。 */
public interface PortfolioPublication {
    Artifact archive(Claim claim, Object draft);
}
