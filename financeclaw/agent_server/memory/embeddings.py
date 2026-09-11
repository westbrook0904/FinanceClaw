"""原生 Store 的 embedding 接入；独立于聊天模型，记录实际查询与索引调用。"""

import logging
from functools import cached_property
from time import monotonic

from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from langchain_openai import OpenAIEmbeddings

from financeclaw.shared.infrastructure.security.egress import EgressPolicy
from financeclaw.shared.infrastructure.settings import FinanceClawSettings

LOGGER = logging.getLogger(__name__)


class ConfiguredEmbeddings(Embeddings):
    """LangGraph Store 直接使用的 Embeddings；不另建索引或全局查询缓存。"""

    @cached_property
    def settings(self):
        """延迟读取配置，模块导入不会触发模型请求。"""
        return FinanceClawSettings()

    @cached_property
    def provider(self):
        """离线模型仅供机制测试；生产必须配置真实 embedding 服务。"""
        settings = self.settings
        if settings.offline_model:
            return DeterministicFakeEmbedding(size=settings.embedding_dimensions)
        if not settings.embedding_model or settings.embedding_api_key is None:
            raise ValueError(
                "configure FINANCECLAW_EMBEDDING_MODEL and FINANCECLAW_EMBEDDING_API_KEY"
            )
        base_url = settings.embedding_base_url or "https://api.openai.com/v1"
        EgressPolicy(
            settings.egress_allowed_hosts,
            require_https=settings.environment.value in {"production", "staging"},
        ).validate(base_url)
        return OpenAIEmbeddings(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            api_key=settings.embedding_api_key,
            base_url=base_url,
            max_retries=0,
            request_timeout=settings.embedding_timeout_seconds,
            check_embedding_ctx_length=False,
        )

    def _measure(self, kind, texts, start, *, success):
        """按查询与文档区分记录调用量、字符数、时长和结果。"""
        LOGGER.info(
            "embedding method=%s count=%d input_chars=%d seconds=%.3f success=%s",
            kind,
            len(texts),
            sum(len(text) for text in texts),
            monotonic() - start,
            success,
        )

    def _validate(self, vectors):
        """拒绝与部署 Store 维度配置不一致的模型输出。"""
        if any(len(vector) != self.settings.embedding_dimensions for vector in vectors):
            raise ValueError("embedding output dimension does not match the Store index")
        return vectors

    def embed_documents(self, texts):
        """为新增或变化的索引内容批量生成向量。"""
        start = monotonic()
        success = False
        try:
            values = self._validate(self.provider.embed_documents(texts))
            success = True
            return values
        finally:
            self._measure("documents", texts, start, success=success)

    def embed_query(self, text):
        """只编码查询文字；已保存文档向量直接参与检索。"""
        start = monotonic()
        success = False
        try:
            value = self._validate([self.provider.embed_query(text)])[0]
            success = True
            return value
        finally:
            self._measure("query", [text], start, success=success)

    async def aembed_documents(self, texts):
        """异步批量索引与同步路径使用相同模型和维度。"""
        start = monotonic()
        success = False
        try:
            values = self._validate(await self.provider.aembed_documents(texts))
            success = True
            return values
        finally:
            self._measure("documents", texts, start, success=success)

    async def aembed_query(self, text):
        """异步查询单独计量，不与后台索引混在一起。"""
        start = monotonic()
        success = False
        try:
            value = self._validate([await self.provider.aembed_query(text)])[0]
            success = True
            return value
        finally:
            self._measure("query", [text], start, success=success)


embeddings = ConfiguredEmbeddings()
