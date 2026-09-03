"""提供 memory eval seed 运维命令的可调用入口。"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from langsmith import Client

DATASET_NAME = "financeclaw-stage3-memory-regression-v1"
SAMPLES = (
    {
        "case": "stable_preference_recall",
        "inputs": {
            "query": "请按我的低波动偏好分析方案",
            "memories": [{"id": "stable", "type": "preference", "content": "用户偏好低波动资产"}],
        },
        "outputs": {"selected_ids": ["stable"], "requires_confirmation": False},
    },
    {
        "case": "superseded_preference",
        "inputs": {
            "query": "我的投资偏好是什么",
            "memories": [
                {"id": "old", "status": "superseded", "content": "偏好成长股"},
                {"id": "new", "status": "active", "content": "偏好价值股"},
            ],
        },
        "outputs": {"selected_ids": ["new"], "requires_confirmation": False},
    },
    {
        "case": "tenant_isolation",
        "inputs": {
            "query": "我的长期目标",
            "trusted_owner": "tenant-a/subject-a",
            "memories": [
                {"id": "owned", "owner": "tenant-a/subject-a"},
                {"id": "foreign", "owner": "tenant-b/subject-a"},
            ],
        },
        "outputs": {"selected_ids": ["owned"], "requires_confirmation": False},
    },
    {
        "case": "current_tool_fact_wins",
        "inputs": {
            "query": "AAPL 现在多少钱",
            "memory": {"id": "stale", "content": "AAPL current price is 100"},
            "tool_result": {"price": "250", "as_of": "2026-09-03T00:00:00Z"},
        },
        "outputs": {"selected_ids": [], "authoritative_price": "250"},
    },
    {
        "case": "high_impact_confirmation",
        "inputs": {
            "draft": {
                "kind": "constraint",
                "content": "用户风险承受能力为高风险",
                "evidence_message_ids": ["current"],
            }
        },
        "outputs": {"selected_ids": [], "requires_confirmation": True},
    },
)


def seed_memory_dataset(client: Client | None = None) -> dict[str, object]:
    """将长期记忆回归样例幂等发布到 LangSmith 数据集。"""
    target = client or Client()
    if target.has_dataset(dataset_name=DATASET_NAME):
        dataset = target.read_dataset(dataset_name=DATASET_NAME)
    else:
        dataset = target.create_dataset(
            DATASET_NAME,
            description=(
                "Stage-3 memory recall, lifecycle, tenant isolation, freshness and approval gate"
            ),
            metadata={"stage": "3", "schema_version": 1},
        )
    examples = [
        {
            "id": uuid5(NAMESPACE_URL, f"{DATASET_NAME}:{sample['case']}"),
            "inputs": sample["inputs"],
            "outputs": sample["outputs"],
            "metadata": {"case": sample["case"], "stage": "3"},
        }
        for sample in SAMPLES
    ]
    target.create_examples(dataset_id=dataset.id, examples=examples)
    return {"dataset_name": DATASET_NAME, "dataset_id": str(dataset.id), "examples": len(examples)}


def main() -> None:
    """解析命令行参数，执行 memory eval seed 操作并输出结果。"""
    print(json.dumps(seed_memory_dataset(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
