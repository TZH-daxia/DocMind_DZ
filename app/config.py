"""应用配置（环境变量前缀 DOCMIND_DZ_）。"""

import re
import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _local_ipv4_addresses() -> list[str]:
    """本机非回环 IPv4 地址（用于放行本机访问的前端来源）。"""

    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address and not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    if not addresses:
        # 主机名解析不到时，用一次 UDP connect 取出口网卡地址（不会真正发包）
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                addresses.append(address)
        except OSError:
            pass
        finally:
            probe.close()
    return addresses


class Settings(BaseSettings):
    """从 .env / 环境变量加载配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DOCMIND_DZ_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "DocMind DZ"
    api_prefix: str = "/dz"
    app_host: str = "127.0.0.1"
    app_port: int = 8010

    data_root: Path = Path("data")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_console: bool = False

    # 单文件上传限制
    max_file_size_mb: int = 50
    max_pages: int = 30
    allowed_extensions: tuple[str, ...] = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
    )

    # 渲染
    render_dpi: int = 200
    # 文本层字符数低于该值视为“扫描件”，需要 VLM 转写
    text_layer_min_chars: int = 180
    force_vlm: bool = False

    # 模型（OpenAI 兼容接口）
    model_api_key: str | None = None
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-flash"
    model_vision_name: str = "deepseek-flash"
    model_temperature: float = 0.0
    model_thinking: bool = False

    fallback_api_key: str | None = None
    fallback_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    fallback_name: str = "qwen-plus"
    fallback_vision_name: str = "qwen-vl-max"

    model_timeout_seconds: float = 180.0
    model_vision_timeout_seconds: float = 120.0
    model_connect_timeout_seconds: float = 10.0
    model_call_retries: int = 1
    model_max_concurrency: int = Field(default=4, ge=1)
    house_bill_concurrency: int = Field(default=3, ge=1)
    max_concurrent_tasks: int = Field(default=4, ge=1)

    # 本地演示模式：不调用真实模型
    mock_llm: bool = False

    # 抽取置信度低于该值的单据进入「待审核」
    review_confidence_threshold: float = 0.7

    # 数据保留
    data_retention_hours: float = 168.0

    # 额外放行的前端来源（逗号分隔，通常留空）：本地 localhost / 127.0.0.1 / 本机 IP
    # 已由 cors_origin_regex 自动放行，只有独立域名部署时才需要在这里追加
    cors_allow_origins: str = ""
    # 自定义跨域来源正则；留空则使用内置本地规则（排除 3005 端口）
    cors_allow_origin_regex: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        """显式来源列表；过滤空项并去重。"""

        origins: list[str] = []
        for item in self.cors_allow_origins.split(","):
            origin = item.strip()
            if origin and origin not in origins:
                origins.append(origin)
        return origins

    @property
    def cors_origin_regex(self) -> str:
        """本地来源匹配规则：本机 localhost / 127.0.0.1 / ::1 / 本机 IPv4 的任意端口。"""

        custom = self.cors_allow_origin_regex.strip()
        if custom:
            return custom
        hosts = ["localhost", r"127\.0\.0\.1", r"\[::1\]"]
        hosts.extend(re.escape(address) for address in _local_ipv4_addresses())
        # 3005 端口显式排除（不再沿用参考项目的默认来源）
        return rf"^https?://({'|'.join(hosts)})(?::(?!3005$)\d+)?$"


@lru_cache
def get_settings() -> Settings:
    """进程级配置单例。"""

    return Settings()
