"""Import layer 3"""
from __future__ import annotations

import aiomysql
import aiosqlite
import asyncio
import functools
import openai
import traceback
import json
import os

from typing import *
from typing_extensions import deprecated
from openai import AsyncOpenAI, AsyncStream
from openai.types.responses import Response, ResponseStreamEvent
from openai.types.create_embedding_response import CreateEmbeddingResponse
from .gvars import *
from .maica_utils import *
from .setting_utils import *
from .fsc_early import *
from .locater import *
# 向量库实现按需导入：vector_store 依赖 lancedb，默认的 SQLite 后端不需要它。

def pkg_init_connection_utils():
    pass


class AiConnectionManager(AsyncCreator):
    """Maintain an AI connection so you don't have to."""


    def __init__(self, api_key, base_url, name='ai_conn', model: Union[int, str]=0, caps: Optional[List[Literal["completion", "embedding", "reranking"]]]=None):
        self.test = False
        self.api_key, self.base_url, self.name, self.model = api_key, base_url, name, model
        self.gen_kwargs = {}
        self.caps = caps or ["completion"]
        """Capabilities. I don't know if there're models can both generate and embed but to be safe."""


    async def _ainit(self):
        if not self.base_url:
            self.test = True
            self.client = None
            self.model_actual = "DISABLED"
            return
        
        else:
            await self.close()
            await self._connect()


    async def _connect(self):
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

        if isinstance(self.model, int):

            model_list = await self.client.models.list()
            models = model_list.data
            
            if not models:
                raise MaicaResponseError(f"{self.name} returned an empty model list")
            self.model_actual = models[0].id

        else:
            self.model_actual = self.model


    def default_params(self, **kwargs):
        """These params will always be applied to generations. Overwritten."""
        self.gen_kwargs = kwargs


    def completions_to_responses(self, **kwargs) -> dict[str, Any]:
        """Build the keyword arguments passed to ``responses.create``."""
        kwargs["model"] = self.model_actual
        mixed_exbody = self.gen_kwargs.get("extra_body", {}) | kwargs.get("extra_body", {})
        mixed_kwargs = self.gen_kwargs | kwargs
        mixed_kwargs["extra_body"] = mixed_exbody

        # Parameters unsupported by the Responses SDK are sent through extra_body.
        for lower_sampling_param in (
            "seed",
            "frequency_penalty",
            "presence_penalty",
        ):
            if lower_sampling_param in mixed_kwargs:
                mixed_kwargs["extra_body"][lower_sampling_param] = mixed_kwargs.pop(lower_sampling_param)

        if "max_tokens" in mixed_kwargs:
            mixed_kwargs["max_output_tokens"] = mixed_kwargs.pop("max_tokens")

        messages = mixed_kwargs.get("input")
        if (
            isinstance(messages, list)
            and messages
            and messages[0]["role"] == "system"
        ):
            messages = messages.copy()
            system = messages.pop(0)
            mixed_kwargs["input"] = messages
            mixed_kwargs["instructions"] = system["content"]

        return mixed_kwargs


    def completions_to_request_body(self, **kwargs) -> dict[str, Any]:
        """
        Build the JSON body ultimately sent to the Responses endpoint.
        Notice: This is a debugging function, not used normally.
        """
        request_kwargs = self.completions_to_responses(**kwargs)
        extra_body = request_kwargs.pop("extra_body", {})

        # These are OpenAI SDK request options rather than JSON body fields.
        for option_name in ("extra_headers", "extra_query", "timeout"):
            request_kwargs.pop(option_name, None)

        request_kwargs.update(extra_body)
        return request_kwargs


    async def _time_counter(self, delay=0):
        await asyncio.sleep(delay)
        time_elps = delay
        while True:
            await asyncio.sleep(5)
            time_elps += 5
            sync_messenger(info=f"A response from {self.model_actual} has delayed for over {time_elps}s", type=MsgType.WARN)


    async def make_completion(self, swallow: Union[bool, str]=False, **kwargs) -> Response | AsyncStream[ResponseStreamEvent]:
        """
        Makes completion with arguments.
        Be cautious that this method is implemented by responses.create instead of completion.create since v1.3.
        swallow: In case some cheap providers are unstable. str as default response.
        """
        if "completion" not in self.caps:
            raise MaicaResponseError("Connected model is not capable of completion")

        mixed_kwargs = self.completions_to_responses(**kwargs)
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg_llm(self.model_actual, mixed_kwargs)
        except Exception:
            pass

        # Uncomment to debug
        # print(json.dumps(mixed_kwargs, ensure_ascii=False))

        try:
            task_stream_resp = asyncio.create_task(self.client.responses.create(**mixed_kwargs))

            # If this is non-streaming task, we allow a longer delay before warning
            if mixed_kwargs.get("stream", False):
                delay = 0
            else:
                delay = 15
            task_time_counter = asyncio.create_task(self._time_counter(delay))

            await asyncio.wait_for(task_stream_resp, timeout=int(G.A.OPENAI_TIMEOUT) if G.A.OPENAI_TIMEOUT != '0' else None)

            resp = task_stream_resp.result()

        except openai.InternalServerError as oe:
            if not swallow:
                raise
            else:
                # Create a fake response
                fake_text = swallow if isinstance(swallow, str) else 'null'
                resp = FakeChatCompletion(fake_text)
                sync_messenger(info=f"Swallowed OpenAI api exception: {str(oe)}, returning default: {fake_text}")
                
        finally:
            task_time_counter.cancel()

        return resp
    

    async def make_embedding(self, **kwargs) -> CreateEmbeddingResponse:
        """As above, just the embedding version."""
        if "embedding" not in self.caps:
            raise MaicaResponseError("Connected model is not capable of embedding")

        kwargs.update(
            {
                "model": self.model_actual,
                "dimensions": G.A.EMBEDDING_DIMS,
            }
        )
        mixed_exbody = {**self.gen_kwargs.get('extra_body', {}), **kwargs.get('extra_body', {})}
        mixed_kwargs = {**self.gen_kwargs, **kwargs}
        mixed_kwargs['extra_body'] = mixed_exbody

        task_resp = asyncio.create_task(self.client.embeddings.create(**mixed_kwargs))
        await asyncio.wait_for(task_resp, timeout=int(G.A.OPENAI_TIMEOUT) if G.A.OPENAI_TIMEOUT != '0' else None)
        resp = task_resp.result()

        return resp
    

    async def make_reranking(self, **kwargs) -> dict:
        """Generate reranking. This is acutally not OpenAI but VLLM standard."""
        if "reranking" not in self.caps:
            raise MaicaResponseError("Connected model is not capable of reranking")

        kwargs.update(
            {
                "model": self.model_actual
            }
        )
        mixed_exbody = {**self.gen_kwargs.get('extra_body', {}), **kwargs.get('extra_body', {})}
        mixed_kwargs = {**self.gen_kwargs, **kwargs}
        mixed_kwargs['extra_body'] = mixed_exbody

        task_resp = asyncio.create_task(
            self.client.post(
                "rerank",
                cast_to=dict[str, Any],
                body=mixed_kwargs,
            )
        )

        await asyncio.wait_for(task_resp, timeout=int(G.A.OPENAI_TIMEOUT) if G.A.OPENAI_TIMEOUT != '0' else None)
        resp = task_resp.result()

        return resp


    async def close(self):
        try:
            await self.client.close()
        except Exception:...


class ConnUtils():
    """Just a wrapping for functions."""

    @staticmethod
    async def vector_pool():
        """向量索引。

        ★ 2026-10-19：默认走 SQLite（一表 + 内存矩阵 + 精确余弦），
           LanceDB 留作回退：设 MAICA_VECTOR_BACKEND=lancedb 即可切回。
           两者接口完全一致（async_create / sync_texts / search / search_with_refs / close）。
        """
        path = G.A.VECTOR_DB_PATH
        if not path:
            return None
        if ExplainUrl(path).is_local and not os.path.isabs(path):
            path = get_inner_path(path)
        backend = (os.environ.get("MAICA_VECTOR_BACKEND") or "sqlite").strip().lower()
        try:
            if backend == "lancedb":
                from maica.maica_utils.vector_store import LanceVectorStore
                return await LanceVectorStore.async_create(
                    path,
                    dimensions=int(G.A.EMBEDDING_DIMS),
                )
            from maica.maica_utils.vector_store_sqlite import SqliteVectorStore
            return await SqliteVectorStore.async_create(
                path,
                dimensions=int(G.A.EMBEDDING_DIMS),
            )
        except Exception as exc:
            sync_messenger(
                info=f"Vector store initialization failed ({backend}); RAG disabled: {exc}",
                type=MsgType.ERROR,
            )
            return None

    @staticmethod
    async def mcore_conn():
        if not G.A.MCORE_ADDR:
            raise CriticalMaicaError("MAICA_MCORE_ADDR is required")
        conn = await AiConnectionManager.async_create(
            api_key=G.A.MCORE_KEY,
            base_url=G.A.MCORE_ADDR,
            name='mcore_conn',
            model=G.A.MCORE_CHOICE or 0,
        )
        conn.default_params(**json.loads(G.A.MCORE_EXTRA))
        return conn

    @staticmethod
    async def mfocus_conn():
        if not G.A.MFOCUS_ADDR:
            raise CriticalMaicaError("MAICA_MFOCUS_ADDR is required")
        conn = await AiConnectionManager.async_create(
            api_key=G.A.MFOCUS_KEY,
            base_url=G.A.MFOCUS_ADDR,
            name='mfocus_conn',
            model=G.A.MFOCUS_CHOICE or 0,
        )
        conn.default_params(**json.loads(G.A.MFOCUS_EXTRA))
        return conn

    @staticmethod
    async def mvista_conn():
        """Disable if no addr provided, or is_mcore_vl."""
        if G.A.MVISTA_ADDR and not is_mcore_vl():
            conn = await AiConnectionManager.async_create(
                api_key=G.A.MVISTA_KEY,
                base_url=G.A.MVISTA_ADDR,
                name='mvista_conn',
                model=G.A.MVISTA_CHOICE or 0,
            )
            conn.default_params(**json.loads(G.A.MVISTA_EXTRA))
            return conn
        else:
            return None

    @staticmethod
    async def mnerve_conn():
        """Disable if no addr provided."""
        if G.A.MNERVE_ADDR:
            conn = await AiConnectionManager.async_create(
                api_key=G.A.MNERVE_KEY,
                base_url=G.A.MNERVE_ADDR,
                name='mnerve_conn',
                model=G.A.MNERVE_CHOICE or 0,
            )
            conn.default_params(**json.loads(G.A.MNERVE_EXTRA))
            return conn
        else:
            return None

    @staticmethod
    async def embedding_conn():
        """Disable if no addr provided."""
        if G.A.EMBEDDING_ADDR:
            conn = await AiConnectionManager.async_create(
                api_key=G.A.EMBEDDING_KEY,
                base_url=G.A.EMBEDDING_ADDR,
                name='embedding_conn',
                model=G.A.EMBEDDING_CHOICE or 0,
                caps=["embedding"],
            )
            conn.default_params(**json.loads(G.A.EMBEDDING_EXTRA))
            return conn
        else:
            return None
        
    @staticmethod
    async def reranking_conn():
        """Disable if no addr provided."""
        if G.A.RERANKING_ADDR:
            conn = await AiConnectionManager.async_create(
                api_key=G.A.RERANKING_KEY,
                base_url=G.A.RERANKING_ADDR,
                name='reranking_conn',
                model=G.A.RERANKING_CHOICE or 0,
                caps=["reranking"],
            )
            conn.default_params(**json.loads(G.A.RERANKING_EXTRA))
            return conn
        else:
            return None

    @staticmethod
    async def extract_conn():
        """事实提取器（本地小模型，OpenAI 兼容端点）。Disable if no addr provided.

        ★ 与 embedding_conn 完全同构：只多一路连接，不改架构。
          后端 venv 里没有 torch / llama_cpp，所以提取器必须是【独立进程】的端点
          （见 _tools/架构与打包约束.md §4.3）。
        """
        if G.A.EXTRACT_ADDR:
            conn = await AiConnectionManager.async_create(
                api_key=G.A.EXTRACT_KEY,
                base_url=G.A.EXTRACT_ADDR,
                name='extract_conn',
                model=G.A.EXTRACT_CHOICE or 0,
                caps=["completion"],
            )
            conn.default_params(**json.loads(G.A.EXTRACT_EXTRA or "{}"))
            return conn
        else:
            return None
