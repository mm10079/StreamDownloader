import asyncio
import shutil
import sys
from pathlib import Path
from typing import Optional

from ..utils import log


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


async def merge_playlist(playlist: Path, output: Path, ffmpeg: str = "ffmpeg") -> bool:
    """以本地 m3u8 合併片段（含解密）為單一檔案"""
    exe = find_ffmpeg(ffmpeg)
    if exe is None:
        await log.error("找不到 ffmpeg，請以 --ffmpeg 指定路徑；片段已保留在 backup 資料夾")
        return False
    if output.exists():
        await log.warning(f"輸出檔已存在，略過合併：{output}")
        return True
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.stem + ".merging" + output.suffix)
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto",
           "-i", playlist.name, "-c", "copy", str(tmp.resolve())]
    await log.info(f"開始合併：{output.name}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(playlist.parent), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        await log.error(f"ffmpeg 合併失敗（{proc.returncode}）：{out.decode(errors='ignore')[-800:]}")
        return False
    tmp.replace(output)
    await log.success(f"合併完成：{output}")
    return True
