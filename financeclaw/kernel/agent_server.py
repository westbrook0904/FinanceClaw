"""Native execution receipt shared by service transports."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServerRun:
    """Agent Server 端一次运行的最小快照。

    使用场景：固定 operation 的回执查找结果，供 BFF 将业务
    run 与 server run 建立映射，并同步运行状态。

    Attributes:
        run_id: 服务端运行 ID，用于后续查询、等待与恢复。
        status: 服务端运行状态字符串（如 pending、running、success、interrupted）。

    """

    run_id: str
    status: str
