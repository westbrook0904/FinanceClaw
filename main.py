"""FinanceClaw 的开发期 HTTP 入口。

真正的 Harness 依赖组装将由 ``harness_bootstrap`` 提供；当前入口仅保留存活检查，
避免在 Contracts Freeze 之前把业务或运行时实现写进应用层。
"""

from fastapi import FastAPI

app = FastAPI(title="FinanceClaw Harness", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """返回应用进程的存活状态。"""

    return {"status": "ok", "phase": "structure-initialized"}
