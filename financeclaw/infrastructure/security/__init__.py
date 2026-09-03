"""安全适配集合：出站网络访问策略等安全基础设施。

本包属于 infrastructure 层，由 bootstrap.py 组合根在启动时用其对全部
出站目标（Provider、JWKS、观测端点、内部 Agent Server）做 allowlist 校验。
"""

from .egress import EgressDenied, EgressPolicy

__all__ = ["EgressDenied", "EgressPolicy"]
