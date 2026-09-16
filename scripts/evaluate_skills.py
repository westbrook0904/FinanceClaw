"""运行固定合成问题的三组技能评测；结果需要人工按证据审阅。"""

import argparse
import json
from pathlib import Path
from time import monotonic
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

from financeclaw.agent_server.bootstrap import build_components
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.skills import SKILL_TOOLS
from financeclaw.shared.turns.types import digest

DATASET = Path(__file__).resolve().parents[1] / "evals/skills-market-brief-v1.json"


class Usage(BaseCallbackHandler):
    """记录实际 Provider 调用的 usage；缺失时保留空值而不是伪造零成本。"""

    def __init__(self):
        """每个问题隔离使用量；不保存完整输入或凭据。"""
        self.calls = 0
        self.usage = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        """在传输边界记录尝试次数，失败也消耗调用机会。"""
        self.calls += 1

    def on_llm_end(self, response, **kwargs):
        """保留供应商观察到的 token 用量。"""
        message = response.generations[0][0].message
        self.usage.append(getattr(message, "usage_metadata", None))


def evaluate(settings, dataset, limit):
    """三组共用业务工具和模型档案，只切换技能入口；不接触生产会话。"""
    results = []
    for variant in dataset["variants"]:
        stack = build_components(
            settings.model_copy(update={"skills_enabled": variant != "disabled"})
        )
        original = stack.default_agent_profile
        profile = original.model_copy(
            update={
                "allowed_tools": tuple(
                    ref
                    for ref in original.allowed_tools
                    if ref.tool_id in {"market_snapshot", "calculate", *SKILL_TOOLS}
                ),
                "worker_manifest": (),
            }
        )
        graph = stack.agent_factory.build(profile)
        for case in dataset["cases"][:limit]:
            usage, started = Usage(), monotonic()
            prompt = ("/skill market-brief " if variant == "explicit" else "") + case["prompt"]
            row = {
                "case_id": case["id"],
                "variant": variant,
                "profile_hash": digest(profile.model_dump(mode="json")),
                "quality_status": "requires_human_review",
            }
            try:
                state = graph.invoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    {"configurable": {"thread_id": str(uuid4())}, "callbacks": [usage]},
                    context=ExecutionContext(
                        tenant_id="skills-evaluation",
                        subject_id="synthetic",
                        turn_id=str(uuid4()),
                        scopes={"market:read", "tools:read"},
                    ),
                )
                row.update(
                    answer=str(state["messages"][-1].content),
                    active=state.get("skill_state", {}).get("active", []),
                    preparation_error=state.get("skill_state", {}).get("preparation_error"),
                    tools=[
                        {"name": m.name, "status": m.status}
                        for m in state["messages"]
                        if m.type == "tool"
                    ],
                    interrupted=bool(state.get("__interrupt__")),
                )
            except Exception as exc:
                row["error_type"] = type(exc).__name__
            row.update(
                elapsed_seconds=round(monotonic() - started, 3),
                model_attempts=usage.calls,
                provider_usage=usage.usage,
            )
            results.append(row)
    return {
        "dataset": dataset["version"],
        "dataset_hash": digest(dataset),
        "live_model": True,
        "scope": (
            "isolated root with market_snapshot demo data and calculate; "
            "no production admission/channel"
        ),
        "results": results,
    }


def main():
    """默认只检查问题集，显式 --live 才调用所配置的供应商。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("build/skills-evaluation.json"))
    args = parser.parse_args()
    dataset = json.loads(DATASET.read_text())
    if not 1 <= args.limit <= len(dataset["cases"]):
        parser.error("--limit must be between 1 and 30")
    if not args.live:
        print(
            json.dumps(
                {
                    "dataset": dataset["version"],
                    "cases": len(dataset["cases"]),
                    "variants": dataset["variants"],
                    "executed": False,
                },
                ensure_ascii=False,
            )
        )
        return
    result = evaluate(
        FinanceClawSettings(offline_model=False, debug_full_io=False), dataset, args.limit
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"已写入 {args.output}；需人工核对质量，未自动判定通过。")


if __name__ == "__main__":
    main()
