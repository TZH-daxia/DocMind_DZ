"""模型提供方抽象：把「用哪家模型」与链路逻辑解耦。

一个 provider = 一套 base_url + key + 模型 ID + 该家专属参数。链路只认识
「按顺序尝试的 provider 列表」，因此加兜底、换模型都不影响业务代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import SecretStr

from app.config import Settings

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI


@dataclass(frozen=True)
class ModelProvider:
    name: str
    api_key: str
    base_url: str
    model: str
    vision_model: str
    temperature: float
    connect_timeout_seconds: float
    read_timeout_seconds: float
    extra_body: dict[str, Any] | None = None

    def build_chat_model(self, *, vision: bool = False) -> ChatOpenAI:
        # langchain_openai 会连带加载整个 openai SDK（约 1s），只在真正调用模型时导入，
        # 避免每次重启服务都为它付出启动耗时
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            api_key=SecretStr(self.api_key),
            base_url=self.base_url,
            model=self.vision_model if vision else self.model,
            temperature=self.temperature,
            timeout=httpx.Timeout(
                self.read_timeout_seconds, connect=self.connect_timeout_seconds
            ),
            max_retries=0,
            extra_body=self.extra_body,
        )


def _thinking_body(enabled: bool) -> dict[str, Any] | None:
    # 只在显式开启时下发，避免把 DeepSeek 专属参数发给其他厂商
    return {"thinking": {"type": "enabled"}} if enabled else None


def build_providers(settings: Settings) -> list[ModelProvider]:
    if not settings.model_api_key:
        raise ValueError("MODEL_API_KEY is required")
    providers = [
        ModelProvider(
            name="primary",
            api_key=settings.model_api_key,
            base_url=settings.model_base_url,
            model=settings.model_name,
            vision_model=settings.model_vision_name or settings.model_name,
            temperature=settings.model_temperature,
            connect_timeout_seconds=settings.model_connect_timeout_seconds,
            read_timeout_seconds=settings.model_timeout_seconds,
            # 思考开关只在显式开启时下发，避免不支持的模型直接 400
            extra_body=_thinking_body(settings.model_thinking),
        )
    ]
    if settings.fallback_api_key:
        providers.append(
            ModelProvider(
                name="fallback",
                api_key=settings.fallback_api_key,
                base_url=settings.fallback_base_url,
                model=settings.fallback_name,
                vision_model=settings.fallback_vision_name or settings.fallback_name,
                temperature=settings.model_temperature,
                connect_timeout_seconds=settings.model_connect_timeout_seconds,
                read_timeout_seconds=settings.model_timeout_seconds,
                extra_body=(
                    {"enable_thinking": True} if settings.model_thinking else None
                ),
            )
        )
    return providers
