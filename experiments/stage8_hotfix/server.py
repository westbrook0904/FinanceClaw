"""隔离 Agent Server 唯一注册入口；不导入生产图或业务服务。"""

import os
from pathlib import Path

from experiments.stage8_hotfix.graphs import Recorder, build_graph

orchestrator = build_graph(recorder=Recorder(Path(os.environ["HF0_EVENT_LOG"])))
