"""模型实际维度必须与原生 Store 配置一致，错误维度不能被截断或补零。"""

import json
from pathlib import Path

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from financeclaw.agent_server.memory.embeddings import ConfiguredEmbeddings
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_dimension_contract_and_mismatch_diagnostic(asynchronous):
    """同步与异步查询、索引都拒绝错误维度，并只报告数字。"""
    dimensions = json.loads(Path("langgraph.json").read_text())["store"]["index"]["dims"]
    assert FinanceClawSettings.model_fields["embedding_dimensions"].default == dimensions == 1024
    embeddings = ConfiguredEmbeddings()
    embeddings.settings = FinanceClawSettings(_env_file=None, embedding_dimensions=dimensions)
    embeddings.provider = DeterministicFakeEmbedding(size=1536)
    for query in (False, True):
        with pytest.raises(ValueError, match=r"expected=1024, actual=\[1536\]"):
            if asynchronous:
                if query:
                    await embeddings.aembed_query("synthetic")
                else:
                    await embeddings.aembed_documents(["synthetic"])
            elif query:
                embeddings.embed_query("synthetic")
            else:
                embeddings.embed_documents(["synthetic"])
