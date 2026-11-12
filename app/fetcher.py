"""统一的 URL 抓取层。

- 支持 http/https（标准库 urllib）与 file:// 协议，方便完全离线演示。
- 所有错误归一化为 FetchError，由上层记录失败地址。
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"抓取失败 {url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str


def _fetch_http(url: str, timeout: float) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            # 报价站为普通公开页面；标识自身有助于站点侧审计。
            "User-Agent": "EnergyPlanComparator/1.0 (+local demo)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read()
            return FetchResult(
                url=response.geturl(),
                status=getattr(response, "status", 200),
                content_type=response.headers.get("Content-Type", ""),
                text=raw.decode(charset, errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        raise FetchError(url, f"HTTP {exc.code}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise FetchError(url, str(exc.reason)) from exc
    except TimeoutError as exc:
        raise FetchError(url, "请求超时") from exc


def _fetch_file(url: str) -> FetchResult:
    path = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return FetchResult(url=url, status=200, content_type="text/html", text=handle.read())
    except FileNotFoundError as exc:
        raise FetchError(url, "本地文件不存在 (HTTP 404)", status=404) from exc
    except OSError as exc:
        raise FetchError(url, str(exc)) from exc


def fetch_url(url: str, timeout: float = 8.0) -> FetchResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https"):
        return _fetch_http(url, timeout)
    if parsed.scheme == "file":
        return _fetch_file(url)
    raise FetchError(url, f"不支持的协议: {parsed.scheme or '(空)'}，仅支持 http/https/file")


def join_url(base: str, link: str) -> str:
    """解析相对链接为绝对地址，同时兼容 file:// 站点。"""
    return urllib.parse.urljoin(base, link)
