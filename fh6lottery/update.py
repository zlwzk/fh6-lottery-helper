"""检查更新。

只在用户主动点「检查更新」时联网（读一次 GitHub Release 接口），
平时不发起任何网络请求。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

REPO = "zlwzk/fh6-lottery-helper"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_LATEST = f"https://github.com/{REPO}/releases/latest"


def check_latest(timeout: float = 6.0) -> tuple[str, str, str]:
    """返回 (最新版本号, 发布页链接, 错误信息)。

    出错时版本号为空字符串，错误信息供界面直接展示。
    """
    request = urllib.request.Request(
        API_LATEST,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "fh6-lottery-helper-updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "", PAGE_LATEST, "仓库里还没有发布任何版本"
        return "", PAGE_LATEST, f"GitHub 返回了 {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "", PAGE_LATEST, f"网络不通（{exc}）"
    except ValueError:
        return "", PAGE_LATEST, "返回内容解析失败"
    if not isinstance(data, dict):
        return "", PAGE_LATEST, "返回内容格式不对"
    tag = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or PAGE_LATEST)
    return tag, url, ""
