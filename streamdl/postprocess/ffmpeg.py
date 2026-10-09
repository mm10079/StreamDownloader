import asyncio
import os
import re
import shutil
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

from ..utils import log
from .progress import REFRESH, hms

_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")


def find_ffmpeg(preferred: str = "ffmpeg") -> Optional[str]:
    """順序：使用者指定 → PATH → 打包後同資料夾 / tools"""
    found = shutil.which(preferred)
    if found:
        return found
    base = Path(getattr(sys, "_MEIPASS", Path(sys.argv[0]).resolve().parent))
    for cand in (base / "ffmpeg.exe", base / "tools" / "ffmpeg.exe"):
        if cand.is_file():
            return str(cand)
    return None


async def run_ffmpeg(exe: str, args: list[str], label: str, cwd: Optional[Path] = None,
                     total: Optional[float] = None, done: Optional[str] = None) -> tuple[int, str]:
    """執行 ffmpeg 並顯示進度條

    - 進度：-progress pipe:1 的 out_time（已輸出的影片時間）與 speed
    - 總長：ffmpeg 讀取輸入時印出的 Duration（多個輸入取最長）；可由 total 預先指定
    回傳 (結束代碼, stderr 最後幾行)
    """
    cmd = [exe, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    progress = await log.Progress.create(label, total=total or 1)
    state = {"total": total or 0.0, "done": 0.0, "speed": ""}
    tail: deque[str] = deque(maxlen=40)

    async def read_stderr():
        async for raw in proc.stderr:
            line = raw.decode(errors="ignore").rstrip()
            tail.append(line)
            m = _DURATION.search(line)
            if m and total is None:
                seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                state["total"] = max(state["total"], seconds)

    async def read_progress():
        last = 0.0
        async for raw in proc.stdout:
            key, _, value = raw.decode(errors="ignore").strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and value.isdigit():
                state["done"] = int(value) / 1_000_000     # 兩者單位實際上都是微秒
            elif key == "speed":
                state["speed"] = value.strip()
            elif key == "progress":
                now = time.monotonic()
                if value == "end" or now - last >= REFRESH:
                    last = now
                    t = state["total"]
                    speed = f"（{state['speed']}）" if state["speed"] and state["speed"] != "N/A" else ""
                    await progress.update(
                        completed=min(state["done"], t) if t else state["done"], total=t or None,
                        message=f"{label} {hms(state['done'])} / {hms(t) if t else '?'} {speed}")

    await asyncio.gather(read_stderr(), read_progress())
    code = await proc.wait()
    if code == 0:
        await progress.done(done or f"{label} 完成")
    else:
        await progress.fail(f"{label} 失敗")
    return code, "\n".join(line for line in tail if line)


def _error_lines(text: str) -> str:
    """從 ffmpeg 的 info 輸出中挑出錯誤相關的行"""
    keys = ("error", "invalid", "fail", "unable", "no such", "denied", "not ")
    lines = [l for l in text.splitlines() if any(k in l.lower() for k in keys)]
    return "\n".join(lines[-8:]) or text[-800:]


async def merge_playlist(playlist: Path, output: Path, ffmpeg: str = "ffmpeg",
                         audio: Optional[Path] = None) -> bool:
    """以本地 m3u8 合併片段（含解密）為單一檔案；audio 為獨立音訊軌的本地 m3u8 時，取 playlist 的影像與 audio 的音訊"""
    exe = find_ffmpeg(ffmpeg)
    if exe is None:
        await log.error("找不到 ffmpeg，請以 --ffmpeg 指定路徑；片段已保留在 backup 資料夾")
        return False
    if output.exists():
        await log.warning(f"輸出檔已存在，略過合併：{output}")
        return True
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.stem + ".merging" + output.suffix)
    input_opts = ["-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto"]
    args = [*input_opts, "-i", playlist.name]
    if audio is not None:
        rel = os.path.relpath(audio, playlist.parent).replace("\\", "/")
        args += [*input_opts, "-i", rel, "-map", "0:v?", "-map", "1:a"]
    code, err = await run_ffmpeg(exe, [*args, "-c", "copy", str(tmp.resolve())],
                                 label=f"合併 {output.name}", cwd=playlist.parent, done=f"合併完成：{output}")
    if code != 0:
        tmp.unlink(missing_ok=True)
        await log.error(f"ffmpeg 合併失敗（{code}）：{_error_lines(err)}")
        return False
    tmp.replace(output)
    return True


async def mux_tracks(inputs: list[Path], output: Path, ffmpeg: str = "ffmpeg") -> bool:
    """將各自獨立的影像 / 音訊軌（例如 DASH）封裝成單一檔案，不重新編碼"""
    exe = find_ffmpeg(ffmpeg)
    if exe is None:
        await log.error("找不到 ffmpeg，請以 --ffmpeg 指定路徑；各軌檔案已保留在 backup 資料夾")
        return False
    if output.exists():
        await log.warning(f"輸出檔已存在，略過合併：{output}")
        return True
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.stem + ".merging" + output.suffix)
    args: list[str] = []
    for p in inputs:
        args += ["-i", str(p.resolve())]
    for i in range(len(inputs)):
        args += ["-map", f"{i}"]
    args += ["-c", "copy", str(tmp.resolve())]
    code, err = await run_ffmpeg(exe, args, label=f"合併 {output.name}", done=f"合併完成：{output}")
    if code != 0:
        tmp.unlink(missing_ok=True)
        await log.error(f"ffmpeg 合併失敗（{code}）：{_error_lines(err)}")
        return False
    tmp.replace(output)
    return True
