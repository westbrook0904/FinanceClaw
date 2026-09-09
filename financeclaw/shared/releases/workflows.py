"""组合复盘的发布声明；与图装配共用，协调端只读取此模块。"""

from financeclaw.kernel.workflows.models import (
    ApprovalPoint,
    WorkflowRelease,
    WorkflowStatus,
    WorkflowTimeoutPolicy,
    WorkflowToolRef,
)
from financeclaw.kernel.workflows.portfolio_review import (
    APPROVAL_POINT,
    MARKET_TOOL_ID,
    MARKET_TOOL_VERSION,
    WORKFLOW_ID,
    PortfolioReviewInput,
    PortfolioReviewOutput,
)


def portfolio_review_release(
    *, run_timeout_seconds: int = 300, approval_timeout_seconds: int = 900
) -> WorkflowRelease:
    """固定工作流的 Schema、工具版本、审批点与运行超时。"""
    return WorkflowRelease(
        workflow_id=WORKFLOW_ID,
        version="1.1.0",
        assistant_id="portfolio_review_v1_1_0",
        input_schema=PortfolioReviewInput,
        output_schema=PortfolioReviewOutput,
        model_profile_id="default@1.0.0",
        allowed_tools=(WorkflowToolRef(tool_id=MARKET_TOOL_ID, version=MARKET_TOOL_VERSION),),
        approval_points=(
            ApprovalPoint(
                approval_id=APPROVAL_POINT,
                description="Approve publishing the point-in-time portfolio review report.",
                requested_action="publish_portfolio_report",
            ),
        ),
        timeout_policy=WorkflowTimeoutPolicy(
            run_timeout_seconds=run_timeout_seconds,
            approval_timeout_seconds=approval_timeout_seconds,
        ),
        status=WorkflowStatus.ACTIVE,
        deployment_revision="portfolio-review-v1/stage6fix-c-1",
        required_scopes=frozenset({"portfolio:review", "market:read"}),
    )
