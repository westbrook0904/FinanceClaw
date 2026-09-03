"""提供 workflow eval seed 运维命令的可调用入口。"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from langsmith import Client

DATASET_NAME = "financeclaw-stage4-published-workflow-v1"
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
    """将工作流回归样例幂等发布到 LangSmith 数据集。"""
    target = client or Client()
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
    examples = [
        {
            "id": uuid5(NAMESPACE_URL, f"{DATASET_NAME}:{sample['case']}"),
            "inputs": sample["inputs"],
            "outputs": sample["outputs"],
            "metadata": {"case": sample["case"], "stage": "4"},
        }
        for sample in SAMPLES
    ]
    target.create_examples(dataset_id=dataset.id, examples=examples)
    return {"dataset_name": DATASET_NAME, "dataset_id": str(dataset.id), "examples": len(examples)}


def main() -> None:
    """解析命令行参数，执行 workflow eval seed 操作并输出结果。"""
    print(json.dumps(seed_workflow_dataset(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
