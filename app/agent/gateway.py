"""统一的模型调用出口。

业务层只调用 `complete` / `complete_vision`，不关心 provider、重试、并发闸门。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.model_call import ProviderCall, call_with_failover
from app.agent.providers import ModelProvider, build_providers
from app.config import Settings

logger = logging.getLogger(__name__)


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        providers: list[ModelProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers or build_providers(settings)
        self._semaphore = asyncio.Semaphore(settings.model_max_concurrency)
        self._clients: dict[tuple[str, bool], object] = {}

    def _client(self, provider: ModelProvider, *, vision: bool):
        key = (provider.name, vision)
        if key not in self._clients:
            self._clients[key] = provider.build_chat_model(vision=vision)
        return self._clients[key]

    async def complete(self, label: str, system_prompt: str, human_prompt: str) -> str:
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        return await self._run(label, messages, vision=False)

    async def complete_vision(
        self,
        label: str,
        system_prompt: str,
        human_prompt: str,
        image_paths: list[str | Path],
    ) -> str:
        content: list[dict[str, object]] = [{"type": "text", "text": human_prompt}]
        for path in image_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _image_data_url(Path(path))},
                }
            )
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=content),  # type: ignore[arg-type]
        ]
        return await self._run(
            label, messages, vision=True, timeout=self.settings.model_vision_timeout_seconds
        )

    async def _run(
        self,
        label: str,
        messages: list[SystemMessage | HumanMessage],
        *,
        vision: bool,
        timeout: float | None = None,
    ) -> str:
        timeout_seconds = timeout or self.settings.model_timeout_seconds

        def make_call(provider: ModelProvider) -> ProviderCall:
            async def factory() -> str:
                async with self._semaphore:
                    model = self._client(provider, vision=vision)
                    response = await model.ainvoke(messages)
                    return _content_to_text(response.content)

            return ProviderCall(
                provider=provider.name,
                label=f"{label}[{provider.name}]",
                factory=factory,
                timeout_seconds=timeout_seconds,
                retries=self.settings.model_call_retries,
            )

        calls = [make_call(provider) for provider in self.providers]
        return await call_with_failover(
            label, calls, should_retry=lambda text: not str(text or "").strip()
        )


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
        return "".join(parts)
    return str(content)


def _image_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_gateway(settings: Settings):
    """按配置构造网关；mock 模式返回固定样例网关。"""

    if settings.mock_llm:
        from app.agent.mock import MockModelGateway

        logger.warning("已启用 MOCK_LLM：不会调用真实模型，仅用于本地演示")
        return MockModelGateway(settings)
    return ModelGateway(settings)
