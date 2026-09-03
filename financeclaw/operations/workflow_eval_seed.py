"""生成 Stage-4 发布工作流（Workflow）评测种子数据集的运维命令。

把正常发布、快照过期分支、瞬时工具故障恢复、审批拒绝与检查点续跑五类
种子用例发布为 LangSmith 数据集，供工作流评测离线回放使用。运行方式：
``python -m financeclaw.operations.workflow_eval_seed``。
"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from langsmith import Client

# Stage-4 工作流评测在 LangSmith 中使用的固定数据集名（含版本号）。
DATASET_NAME = "financeclaw-stage4-published-workflow-v1"
# 五类工作流评测种子用例：正常发布、过期分支、工具恢复、审批拒绝与断点续跑。
SAMPLES = (
    {
        "case": "normal_publication",
        "inputs": {"snapshots": "fresh", "decision": "approve"},
        "outputs": {"status": "completed", "artifact_count": 1},
    },
    {
        "case": "stale_snapshot_branch",
        "inputs": {"snapshots": "stale", "decision": None},
        "outputs": {"status": "failed", "approval_requested": False},
    },
    {
        "case": "transient_tool_recovery",
        "inputs": {"market_failures_before_success": 1, "decision": "approve"},
        "outputs": {"status": "completed", "market_attempts": 2},
    },
    {
        "case": "approval_rejected",
        "inputs": {"snapshots": "fresh", "decision": "reject"},
        "outputs": {"status": "rejected", "artifact_count": 0},
    },
    {
        "case": "checkpoint_resume",
        "inputs": {"restart_after_interrupt": True, "decision": "approve"},
        "outputs": {"status": "completed", "workflow_version": "1.0.0"},
    },
)


def seed_workflow_dataset(client: Client | None = None) -> dict[str, object]:
    """把工作流评测种子用例发布到固定名称的 LangSmith 数据集。

    Args:
        client: 可注入的 LangSmith 客户端；缺省时现场构造默认客户端。

    Returns:
        含数据集名、数据集 ID 与示例数量的发布摘要。

    """
    # 1. 使用注入客户端或默认 LangSmith 客户端。
    target = client or Client()
    # 2. 数据集已存在则复用，否则按固定名称与元数据创建。
    if target.has_dataset(dataset_name=DATASET_NAME):
        dataset = target.read_dataset(dataset_name=DATASET_NAME)
    else:
        dataset = target.create_dataset(
            DATASET_NAME,
            description=(
                "Stage-4 normal, branch, tool recovery, approval rejection and resume cases"
            ),
            metadata={"stage": "4", "schema_version": 1},
        )
    # 3. 用 uuid5 基于（数据集名，用例名）生成确定性示例 ID，保证跨次运行稳定。
    examples = [
        {
            "id": uuid5(NAMESPACE_URL, f"{DATASET_NAME}:{sample['case']}"),
            "inputs": sample["inputs"],
            "outputs": sample["outputs"],
            "metadata": {"case": sample["case"], "stage": "4"},
        }
        for sample in SAMPLES
    ]
    # 4. 灌入全部示例并返回发布摘要。
    target.create_examples(dataset_id=dataset.id, examples=examples)
    return {"dataset_name": DATASET_NAME, "dataset_id": str(dataset.id), "examples": len(examples)}


def main() -> None:
    """发布工作流评测种子数据集并打印 JSON 摘要。"""
    # 1. 执行种子命令并打印 JSON 结果。
    print(json.dumps(seed_workflow_dataset(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
