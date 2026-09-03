"""出站访问策略：按主机 allowlist 与 HTTPS 约束校验出站 URL。

本模块属于 infrastructure 层的安全适配：启动时与运行期据此校验外部
目标，限制可达主机，缓解 SSRF 与数据外泄风险。
"""

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse


class EgressDenied(ValueError):
    """出站 URL 未通过 allowlist 校验时抛出的异常。

    使用场景：``EgressPolicy.validate()`` 校验失败时抛出，由组合根在
    启动期或调用方在运行期捕获处理；继承 ``ValueError`` 便于按配置错误
    统一归类。
    """

    pass


def _normalized_host(host: str) -> str:
    """规范化主机名：去除首尾空白与末尾的根点，并转为小写。

    Args:
        host: 原始主机名。

    Returns:
        规范化后的主机名，保证 allowlist 比对不受大小写与书写差异影响。

    """
    return host.strip().rstrip(".").lower()


def _is_private_host(host: str) -> bool:
    """判断主机是否为 localhost 或私有/回环/链路本地地址。

    使用场景：``allow_private_hosts`` 开启时放行内部服务目标
    （如 Agent Server）；对外部 allowlist 校验时用于收紧内网访问。

    Args:
        host: 待判断的主机名或 IP 字面量。

    Returns:
        属于内部/私有目标返回 True；非 IP 字面量返回 False。

    """
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """不可变的出站访问策略：声明放行主机与 HTTPS、内网约束。

    使用场景：bootstrap.py 启动时构造多个策略实例，分别校验模型
    Provider、JWKS 与观测端点（外部 allowlist）以及内部 Agent Server
    （允许内网与 HTTP）。

    Attributes:
        allowed_hosts: 放行的主机名集合（比对前会做规范化）。
        require_https: 是否强制 HTTPS，默认 True；内部服务可放宽。
        allow_private_hosts: 是否允许私有/回环/链路本地主机，默认禁止。

    """

    allowed_hosts: frozenset[str]
    require_https: bool = True
    allow_private_hosts: bool = False

    def validate(self, url: str) -> str:
        """校验一个出站 URL 是否符合本策略，通过则原样返回。

        使用场景：组合根对 ``provider_base_url``、``oidc_jwks_url`` 等配置
        逐一校验；任何不合规的 URL 都会在启动期失败（fail fast）。

        Args:
            url: 待校验的出站 URL。

        Returns:
            校验通过的原 URL（便于在表达式内联使用）。

        Raises:
            EgressDenied: 缺少主机名、携带 userinfo、协议不符、
                强制 HTTPS 时使用 HTTP，或主机不在放行名单内。

        """
        # 1. 解析 URL 并规范化主机名，同时规范化 allowlist 便于比对。
        parsed = urlparse(url)
        host = _normalized_host(parsed.hostname or "")
        allowed = {_normalized_host(value) for value in self.allowed_hosts}
        # 2. 拒绝缺少主机名或携带 userinfo（凭证泄露风险）的 URL。
        if not host or parsed.username or parsed.password:
            raise EgressDenied("outbound URL must contain a hostname and no userinfo")
        # 3. 协议必须是 HTTP/HTTPS，且按策略强制 HTTPS。
        if parsed.scheme not in {"http", "https"}:
            raise EgressDenied("outbound URL must use HTTP or HTTPS")
        if self.require_https and parsed.scheme != "https":
            raise EgressDenied("outbound URL must use HTTPS")
        # 4. 主机必须在 allowlist 内，或在显式允许时属于内部/私有目标。
        if host not in allowed and not (self.allow_private_hosts and _is_private_host(host)):
            raise EgressDenied(f"outbound host is not allowlisted: {host}")
        return url
