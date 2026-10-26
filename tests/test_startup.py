"""启动相关：模型 SDK 延迟导入，以及终端启动提示。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.config import Settings, _local_ipv4_addresses
from app.main import _display_hosts, format_startup_banner


def test_startup_banner_lists_addresses_and_paths(tmp_path: Path) -> None:
    settings = Settings(
        app_host="0.0.0.0",
        app_port=8010,
        log_dir=tmp_path / "logs",
        data_root=tmp_path / "data",
    )

    banner = format_startup_banner(settings)

    assert "http://127.0.0.1:8010/" in banner
    assert "http://127.0.0.1:8010/docs" in banner
    assert "http://127.0.0.1:8010/health" in banner
    assert str((tmp_path / "logs" / "app.log").resolve()) in banner
    assert str((tmp_path / "data" / "tasks").resolve()) in banner
    # 模型模式不再展示在终端横幅里（改由启动日志记录）
    assert "模型模式" not in banner
    # 实时看日志的命令按平台给：Linux（容器部署）不能用 PowerShell 写法
    assert ("Get-Content" if os.name == "nt" else "tail -f") in banner
    # 绑 0.0.0.0 时，除回环地址外还给出局域网地址（本机取不到网卡 IP 时跳过）
    if _local_ipv4_addresses():
        assert "局域网访问：" in banner


def test_display_hosts_include_loopback_and_lan() -> None:
    hosts = _display_hosts("0.0.0.0")

    assert hosts[0] == "127.0.0.1"
    assert hosts == _display_hosts("::")
    # 绑具体地址时原样展示
    assert _display_hosts("192.168.1.10") == ["192.168.1.10"]


def test_app_import_does_not_load_openai_sdk() -> None:
    code = "import sys, app.main; print('langchain_openai' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False", "langchain_openai 应在真正调用模型时才导入"
