"""OCR —— 复用 Windows 自带的识别引擎（Windows.Media.Ocr），零额外安装。

实现方式：拉起一个常驻的 PowerShell 进程走 WinRT OCR（进程内只初始化一次引擎），
Python 通过 stdin/stdout 交换 JSON。识别失败时静默降级，不影响主流程。
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Iterable

import numpy as np

from . import paths

CREATE_NO_WINDOW = 0x08000000

SERVER_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $t) {
    $m = $asTaskGeneric.MakeGenericMethod($t)
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

$script:engines = @{}

function Get-Engine([string]$tag) {
    if ($script:engines.ContainsKey($tag)) { return $script:engines[$tag] }
    $lang = New-Object Windows.Globalization.Language $tag
    $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($null -eq $eng) { $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    $script:engines[$tag] = $eng
    return $eng
}

function Invoke-Ocr([string]$path, [string]$tag) {
    $engine = Get-Engine $tag
    if ($null -eq $engine) { return $null }
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        return $result.Text
    } finally {
        $stream.Dispose()
    }
}

$langs = @()
try {
    foreach ($l in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) { $langs += $l.LanguageTag }
} catch { }
[Console]::Out.WriteLine((@{ ok = $true; ready = $true; langs = $langs } | ConvertTo-Json -Compress))
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '') { continue }
    if ($line -eq '__exit__') { break }
    try {
        $req = $line | ConvertFrom-Json
        if ($req.cmd -eq 'ping') {
            $payload = @{ ok = $true; text = '' }
        } else {
            $text = Invoke-Ocr ([string]$req.path) ([string]$req.lang)
            if ($null -eq $text) { $payload = @{ ok = $false; error = 'no-ocr-engine' } }
            else { $payload = @{ ok = $true; text = $text } }
        }
    } catch {
        $payload = @{ ok = $false; error = $_.Exception.Message }
    }
    [Console]::Out.WriteLine(($payload | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
}
'''

READY_TIMEOUT = 25.0
CALL_TIMEOUT = 6.0


class OcrError(RuntimeError):
    pass


class OcrClient:
    """常驻 OCR 客户端；线程安全。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._languages: list[str] = []
        self._ready = False
        self._error = ""
        self._frame_dir = paths.TEMP_DIR
        self._seq = 0
        self._last_call = 0.0
        self._avg_ms = 0.0

    # ---------------- 生命周期 ----------------
    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def error(self) -> str:
        return self._error

    @property
    def languages(self) -> list[str]:
        return list(self._languages)

    @property
    def avg_ms(self) -> float:
        return self._avg_ms

    def start(self, wait: bool = True) -> bool:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return self._ready
            script = paths.DATA_DIR / "ps_ocr_server.ps1"
            try:
                paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
                script.write_text(SERVER_SCRIPT, encoding="utf-8-sig")
            except OSError as exc:
                self._error = f"无法写入 OCR 脚本：{exc}"
                return False
            try:
                for stale in self._frame_dir.glob("ocr_*.png"):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            except OSError:
                pass
            try:
                self._proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive",
                     "-ExecutionPolicy", "Bypass", "-File", str(script)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                self._error = f"无法启动 PowerShell：{exc}"
                return False

            threading.Thread(target=self._pump, args=(self._proc,),
                             daemon=True, name="ocr-reader").start()

        if not wait:
            return True

        try:
            first = self._lines.get(timeout=READY_TIMEOUT)
        except queue.Empty:
            self._error = "OCR 引擎启动超时"
            self.stop()
            return False
        if not first:
            self._error = "OCR 引擎意外退出"
            return False
        try:
            data = json.loads(first)
            self._languages = list(data.get("langs") or [])
            self._ready = True
            self._error = ""
        except ValueError:
            self._error = "OCR 引擎返回异常数据"
            return False
        return True

    def _pump(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        self._lines.put(None)

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._ready = False
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write("__exit__\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def restart(self) -> bool:
        self.stop()
        return self.start(wait=True)

    # ---------------- 识别 ----------------
    def recognize(self, image: np.ndarray, lang: str = "zh-Hans-CN",
                  scale: float = 1.0, threshold: int = 0,
                  attempts: int = 2) -> str | None:
        """识别一张 BGR 图，返回纯文本（失败返回 None）。

        WinRT 读同一个文件路径时会偶发「发生一个或多个错误」（文件被占用/解码超时），
        所以每次换一个临时文件名，并在失败时自动重试一次。
        """
        if image is None or getattr(image, "size", 0) == 0:
            return None
        if not self._ready:
            return None
        prepared = self._preprocess(image, scale, threshold)
        if prepared is None:
            return None
        tries = max(1, int(attempts))
        for index in range(tries):
            text = self._recognize_once(prepared, lang)
            if text is not None:
                return text
            if index + 1 < tries:
                time.sleep(0.12)
        return None

    def _recognize_once(self, image: np.ndarray, lang: str) -> str | None:
        with self._lock:
            self._seq += 1
            seq = self._seq
            path = self._frame_dir / f"ocr_{self._seq % 128:03d}.png"
        try:
            self._frame_dir.mkdir(parents=True, exist_ok=True)
            self._write_png(image, path)
        except Exception as exc:
            self._error = f"OCR 临时图写入失败：{exc}"
            return None

        request = json.dumps({"path": str(path), "lang": lang,
                              "cmd": "ocr", "seq": seq}, ensure_ascii=False)
        started = time.perf_counter()
        with self._lock:
            if not self._proc or self._proc.poll() is not None or self._proc.stdin is None:
                self._ready = False
                self._error = "OCR 引擎已退出"
                return None
            try:
                self._proc.stdin.write(request + "\n")
                self._proc.stdin.flush()
            except Exception as exc:
                self._error = f"OCR 通信失败：{exc}"
                self._ready = False
                return None
            reply = self._read_reply(seq)

        elapsed = (time.perf_counter() - started) * 1000.0
        if not reply:
            self._error = "OCR 无响应"
            return None
        self._last_call = time.time()
        self._avg_ms = self._avg_ms * 0.7 + elapsed * 0.3
        try:
            data = json.loads(reply)
        except ValueError:
            return None
        if not data.get("ok"):
            self._error = str(data.get("error") or "OCR 失败")
            return None
        return str(data.get("text") or "")

    def _read_reply(self, seq: int) -> str | None:
        """只接受本轮的回复。

        如果某次调用超时，它的回复会晚到并残留在队列里；下一次调用又把它读走，
        就会把上一帧的文字当成当前画面。所以这里按序号严格匹配，不匹配的直接丢掉。
        """
        deadline = time.time() + CALL_TIMEOUT
        while time.time() < deadline:
            try:
                line = self._lines.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                self._ready = False
                return None
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            try:
                got = int(data.get("seq"))
            except (TypeError, ValueError):
                # 启动握手等没有序号的包
                continue
            if got != seq:
                continue
            return line
        return None

    @staticmethod
    def _write_png(image: np.ndarray, path) -> None:
        try:
            import cv2
            cv2.imwrite(str(path), image)
            return
        except Exception:
            pass
        from PIL import Image
        Image.fromarray(image[:, :, ::-1]).save(path)

    @staticmethod
    def _preprocess(image: np.ndarray, scale: float, threshold: int) -> np.ndarray | None:
        try:
            import cv2
        except ImportError:  # pragma: no cover
            return None
        img = image
        if scale and abs(scale - 1.0) > 0.01:
            img = cv2.resize(img, None, fx=float(scale), fy=float(scale),
                             interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if threshold > 0:
            _t, gray = cv2.threshold(gray, int(threshold), 255, cv2.THRESH_BINARY)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


_instance: OcrClient | None = None
_instance_lock = threading.Lock()


def get_ocr() -> OcrClient:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = OcrClient()
        return _instance


def warm_up() -> None:
    """后台预热，避免第一次识别时卡住。"""
    def _run():
        client = get_ocr()
        if not client.ready:
            client.start(wait=True)
    threading.Thread(target=_run, daemon=True, name="ocr-warmup").start()


# --------------------------------------------------------------------------- #
# 文本解析小工具
# --------------------------------------------------------------------------- #
def first_int(text: str | None, pattern: str = r"\d+") -> int | None:
    if not text:
        return None
    import re
    match = re.search(pattern, text.replace("\u00a0", " "))
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def contains_any(text: str | None, keywords: Iterable[str]) -> str | None:
    if not text:
        return None
    flat = "".join(text.split()).lower()
    for kw in keywords:
        if kw and "".join(kw.split()).lower() in flat:
            return kw
    return None
