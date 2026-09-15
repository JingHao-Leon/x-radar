"""环境变量配置，全部带 XR_ 前缀。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"XR_{name}", default).strip()


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: Path = field(default_factory=lambda: Path("./data").resolve())
    poll_interval: int = 60
    proxy: str | None = None  # None=不设置(读 env), "off"=强制直连, 其他=显式地址
    token: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    headless: bool = True
    cdp_endpoint: str = ""
    max_pages: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radar.db"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)


def detect_system_proxy() -> str | None:
    """尽力探测本机 HTTP 代理（macOS scutil，其次常见环境变量）。"""
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if env_proxy:
        return env_proxy
    if sys_platform() != "darwin":
        return None
    try:
        import subprocess

        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=3
        ).stdout
        if "HTTPSEnable : 1" in out:
            host = "127.0.0.1"
            port = ""
            for line in out.splitlines():
                if "HTTPSProxy" in line:
                    host = line.split(":")[-1].strip()
                if "HTTPSPort" in line:
                    port = line.split(":")[-1].strip()
            if host and port:
                return f"http://{host}:{port}"
    except Exception:
        return None
    return None


def sys_platform() -> str:
    import sys

    return sys.platform


def load_settings() -> Settings:
    s = Settings(
        host=_env("HOST", "127.0.0.1"),
        port=int(_env("PORT", "8787")),
        data_dir=Path(_env("DATA_DIR", "./data")).expanduser().resolve(),
        poll_interval=max(30, int(_env("POLL_INTERVAL", "60"))),
        token=_env("TOKEN"),
        llm_base_url=_env("LLM_BASE_URL").rstrip("/"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        headless=_env("HEADLESS", "1") not in ("0", "false", "False"),
        cdp_endpoint=_env("CDP_ENDPOINT"),
        max_pages=max(0, int(_env("MAX_PAGES", "3"))),
    )
    raw_proxy = _env("PROXY", "auto")
    if raw_proxy == "off":
        s.proxy = None
    elif raw_proxy == "auto":
        s.proxy = detect_system_proxy()
    else:
        s.proxy = raw_proxy
    s.data_dir.mkdir(parents=True, exist_ok=True)
    (s.media_dir / "tweets").mkdir(parents=True, exist_ok=True)
    (s.media_dir / "pages").mkdir(parents=True, exist_ok=True)
    return s
