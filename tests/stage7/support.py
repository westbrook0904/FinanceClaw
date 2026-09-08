"""紫微合成资料与隔离组件；安装项目 ziwei extra 后运行引擎集成测试。"""

import json

import pytest

from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.ziwei import ZiweiAnalysisRequest
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from tests.support import build_components

KEY = "synthetic-stage7-fixture-key-not-for-real-use"


def settings(**changes) -> FinanceClawSettings:
    """显式启用候选并关闭所有原文调试，不从 .env 读取密钥。"""
    return FinanceClawSettings(
        _env_file=None,
        **{
            "environment": "test",
            "offline_model": True,
            "debug_full_io": False,
            "ziwei_enabled": True,
            "ziwei_convention": "x-iztro-civil-candidate@1.0.0",
            "ziwei_hmac_key": KEY,
            "langsmith_hide_inputs": True,
            "langsmith_hide_outputs": True,
            **changes,
        },
    )


def context(**changes) -> ExecutionContext:
    """冻结的合成执行身份，没有实际个人资料。"""
    return ExecutionContext(
        **{
            "tenant_id": "synthetic-tenant",
            "subject_id": "synthetic-owner",
            "run_id": "ziwei-child",
            "turn_id": "turn-ziwei",
            "conversation_id": "ziwei-conversation",
            "request_clock": "2026-09-06T01:00:00+08:00",
            "data_classification": "confidential",
            "scopes": {"ziwei:read", "artifacts:read"},
            **changes,
        }
    )


def request(**changes) -> ZiweiAnalysisRequest:
    """使用公开引擎示例生日，非真实用户记录。"""
    return ZiweiAnalysisRequest.model_validate(
        {
            "question": "请查看这一日的盘面",
            "mode": "chart_only",
            "birth": {
                "calendar": "solar",
                "date": {"year": 2000, "month": 8, "day": 16},
                "time": {"kind": "clock", "clock": "03:30"},
                "time_basis": "civil",
                "place": {"name": "上海"},
                "sex_for_chart": "female",
            },
            "level": "daily",
            "target": {"kind": "point", "on_date": "2026-09-06"},
            **changes,
        }
    )


def envelope(value: ZiweiAnalysisRequest) -> dict:
    """与 DelegationService 实际发送的 child 输入结构一致。"""
    return {
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": value.question,
                        "arguments": value.model_dump(mode="json"),
                        "authorized_context": [],
                    },
                    ensure_ascii=False,
                ),
            }
        ]
    }


def components(tmp_path=None):
    """未安装可选依赖时只跳过真实引擎测试，默认金融链路测试仍必须运行。"""
    pytest.importorskip("x_iztro")
    pytest.importorskip("tzdata")
    overrides = {}
    if tmp_path:
        overrides = {
            "database_url": f"sqlite+pysqlite:///{tmp_path / 'stage7.sqlite'}",
            "artifact_root": str(tmp_path / "artifacts"),
        }
    return build_components(settings(**overrides), enable_persistence=bool(tmp_path))
