"""校验外部 URL 是否满足协议、主机白名单和私网限制。"""

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse


class EgressDenied(ValueError):
    """定义EgressDenied。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """


def _normalized_host(host: str) -> str:
    """规范化 URL 主机名，去除大小写和尾随点差异。"""
    return host.strip().rstrip(".").lower()


def _is_private_host(host: str) -> bool:
    """判断egress 模块的数据是否满足对应条件。"""
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """在创建网络客户端前验证 URL 的协议、主机白名单和私网属性。

    适用场景：
        用于在执行副作用前作出确定性治理决策。

    属性：
        allowed_hosts: 当前配置明确允许的值集合。
        require_https: 是否拒绝所有非 HTTPS 出站 URL。
        allow_private_hosts: 是否允许目标解析为回环、链路本地或私网地址。
    """

    allowed_hosts: frozenset[str]
    require_https: bool = True
    allow_private_hosts: bool = False

    def validate(self, url: str) -> str:
        """规范化 URL，并依次校验 HTTPS、主机白名单和私网地址限制。"""
        parsed = urlparse(url)
        host = _normalized_host(parsed.hostname or "")
        allowed = {_normalized_host(value) for value in self.allowed_hosts}
        if not host or parsed.username or parsed.password:
            raise EgressDenied("outbound URL must contain a hostname and no userinfo")
        if parsed.scheme not in {"http", "https"}:
            raise EgressDenied("outbound URL must use HTTP or HTTPS")
        if self.require_https and parsed.scheme != "https":
            raise EgressDenied("outbound URL must use HTTPS")
        if host not in allowed and not (self.allow_private_hosts and _is_private_host(host)):
            raise EgressDenied(f"outbound host is not allowlisted: {host}")
        return url
