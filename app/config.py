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
    api_prefix: str = "/docmind_dz"
    app_host: str = "127.0.0.1"
    app_port: int = 8010

    data_root: Path = Path("data")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_console: bool = False
    # 系统日志按天轮转，保留天数（同时限制历史日志占用的磁盘）
    log_retention_days: int = 14

    # 单文件上传限制
    max_file_size_mb: int = 50
    # 一次上传的文件数上限（前端一个文件一个任务，合并上传时同样受限）
    max_upload_files: int = 5
    max_pages: int = 30
    # 暂不支持图片文件（png/jpg 等）
    allowed_extensions: tuple[str, ...] = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
    )

    # 渲染
    render_dpi: int = 200
    # Word/Excel 是否用 LibreOffice 转 PDF 后渲染页图（关闭则只抽文本，无页面图片）
    office_render_enabled: bool = True


    # 模型（OpenAI 兼容接口）
    model_api_key: str | None = None
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-flash"
    model_vision_name: str = "deepseek-flash"
    model_temperature: float = 0.0
    # 字段提取/分类开思考：候选更稳，代价是耗时明显变长（可按需用环境变量关掉）
    model_thinking: bool = True
    # 识图转写关思考：逐字转写不需要推理，开启后更慢，实测准确率差异很小
    model_vision_thinking: bool = False

    fallback_api_key: str | None = None
    fallback_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    fallback_name: str = "qwen-plus"
    fallback_vision_name: str = "qwen-vl-max"

    # 文本调用（字段抽取/分类）超时：开思考后耗时明显变长，留足余量
    model_timeout_seconds: float = 300.0
    model_vision_timeout_seconds: float = 120.0
    model_connect_timeout_seconds: float = 10.0
    model_call_retries: int = 1
    # 模型调用总并发（文本 + 视觉共用），超限排队；排队时间不计入调用超时
    model_max_concurrency: int = Field(default=16, ge=1)
    # 同一任务内多份分单的并行抽取数
    house_bill_concurrency: int = Field(default=3, ge=1)
    # 视觉转写逐页并发（实际还受 model_max_concurrency 限制）
    vision_page_concurrency: int = Field(default=2, ge=1)
    # LibreOffice 转 PDF 并发：soffice 单实例，默认串行
    office_concurrency: int = Field(default=1, ge=1)
    # 同时执行的任务数，超限排队（快照保持 queued）
    max_concurrent_tasks: int = Field(default=5, ge=1)
    # provider 熔断：连续失败 N 次进入冷却，冷却期内直接走兜底
    model_circuit_failures: int = Field(default=3, ge=1)
    model_circuit_cooldown_seconds: float = 90.0

    # 抽取置信度低于该值的单据进入「待审核」
    review_confidence_threshold: float = 0.7

    # 数据保留
    data_retention_hours: float = 168.0

    # 开发调试用：true 时允许任意来源（生产环境不要打开）
    cors_allow_all: bool = False
    # 额外放行的前端来源（逗号分隔，通常留空）：本地 localhost / 127.0.0.1 / 本机 IP
    # 已由 cors_origin_regex 自动放行，只有独立域名部署时才需要在这里追加
    cors_allow_origins: str = ""
    # 自定义跨域来源正则；留空则使用内置本地规则（排除 3005 端口）
    cors_allow_origin_regex: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        """显式来源列表；过滤空项并去重。开发开关打开时返回通配。"""

        if self.cors_allow_all:
            return ["*"]
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
