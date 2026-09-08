"""Native HF-1 test server: production graphs with synthetic models, quotes and identities."""

import json
from pathlib import Path

from tests.stage6fix.test_batch_tools import call
from tests.stage7.support import request
from tests.stage8_hotfix.test_production_subgraphs import portfolio, root_graph, stack

# Hold this test fixture's resource lifetime until the isolated server process exits.
_resources = stack.__wrapped__(Path.cwd())
components = next(_resources)
root, kwargs = root_graph(
    components,
    [
        call("call_agent__market_research_agent", 1, task="bounded synthetic research"),
        portfolio(2),
        call(
            "call_agent__ziwei_doushu_agent",
            3,
            task="合成盘面",
            arguments=request(mode="interpretation").model_dump(mode="json"),
        ),
        call("call_agent__market_research_agent", 4, task="second bounded research"),
        portfolio(5),
    ],
    questions=1,
    native=True,
)
Path("hf1-context.json").write_text(json.dumps(kwargs["context"].model_dump(mode="json")))
