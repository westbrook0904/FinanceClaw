"""生成 Stage-3 记忆能力评测种子数据集的运维命令。

把记忆召回、生命周期（被取代偏好）、租户隔离、金融时效与高危确认五类
种子用例发布为 LangSmith 数据集，供记忆评测离线回放使用。运行方式：
``python -m financeclaw.operations.memory_eval_seed``。
"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from langsmith import Client

# Stage-3 记忆评测在 LangSmith 中使用的固定数据集名（含版本号）。
DATASET_NAME = "financeclaw-stage3-memory-regression-v1"
# 五类记忆评测种子用例：稳定偏好召回、被取代偏好、租户隔离、现价优先与高危确认。
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
    """把记忆评测种子用例发布到固定名称的 LangSmith 数据集。

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
                "Stage-3 memory recall, lifecycle, tenant isolation, freshness and approval gate"
            ),
            metadata={"stage": "3", "schema_version": 1},
        )
    # 3. 用 uuid5 基于（数据集名，用例名）生成确定性示例 ID，保证跨次运行稳定。
    examples = [
        {
            "id": uuid5(NAMESPACE_URL, f"{DATASET_NAME}:{sample['case']}"),
            "inputs": sample["inputs"],
            "outputs": sample["outputs"],
            "metadata": {"case": sample["case"], "stage": "3"},
        }
        for sample in SAMPLES
    ]
    # 4. 灌入全部示例并返回发布摘要。
    target.create_examples(dataset_id=dataset.id, examples=examples)
    return {"dataset_name": DATASET_NAME, "dataset_id": str(dataset.id), "examples": len(examples)}


def main() -> None:
    """发布记忆评测种子数据集并打印 JSON 摘要。"""
    # 1. 执行种子命令并打印 JSON 结果。
    print(json.dumps(seed_memory_dataset(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
