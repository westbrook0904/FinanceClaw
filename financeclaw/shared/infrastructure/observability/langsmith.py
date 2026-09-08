"""LangSmith 接入：通过环境变量配置追踪项目、采样率与输入输出隐藏。

本模块属于 infrastructure 层的观测适配：LangChain/LangGraph 运行时会
读取这些环境变量上报追踪，生产环境隐藏输入输出以满足合规要求。
"""

import os

import langsmith


def configure_langsmith(
    *,
    project: str,
    endpoint: str,
    sample_rate: float,
    hide_inputs: bool,
    hide_outputs: bool,
) -> None:
    """按配置设置 LangSmith 环境变量并完成 SDK 初始化。

    使用场景：bootstrap.py 组合根启动时调用；配置须在导入/构造任何
    LangChain 组件之前生效，否则运行时会以默认值上报。

    Args:
        project: 追踪上报的项目名。
        endpoint: LangSmith API 端点（启动时已通过出站 allowlist 校验）。
        sample_rate: 追踪采样率 [0, 1]，生产不得超过 0.1。
        hide_inputs: 是否在上报内容中隐藏输入。
        hide_outputs: 是否在上报内容中隐藏输出。

    """
    # 1. 通过环境变量下发采样与脱敏开关，供 LangChain/LangGraph 运行时读取。
    os.environ["LANGSMITH_PROJECT"] = project
    os.environ["LANGSMITH_ENDPOINT"] = endpoint
    os.environ["LANGSMITH_TRACING_SAMPLING_RATE"] = str(sample_rate)
    os.environ["LANGSMITH_HIDE_INPUTS"] = str(hide_inputs).lower()
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = str(hide_outputs).lower()
    # 2. 初始化 SDK 使项目配置生效。
    langsmith.configure(project_name=project)
