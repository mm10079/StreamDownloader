# StreamDownloader

串流下載器：支援 **HLS（m3u8）** 與 **DASH（mpd）**，可下載直播與存檔。支援的網站可自動登入並找出串流，其他網站可用瀏覽器監控自動找出串流。

## 功能

- **HLS / DASH**：可選畫質，影像與音訊分軌時自動一併下載並合併
- **直播**：持續追蹤直到直播結束；可選擇回溯下載播放清單以外的較早片段
- **加密內容**：一般 HLS 加密自動解密；DASH ClearKey 自動取得金鑰；持有金鑰時可用 `--key` 解密
- **斷點續傳**：中斷後重新執行相同指令即可接續
- **瀏覽器監控**：沒有網址或網站不支援時，開啟瀏覽器偵測頁面中的串流，逐一詢問是否下載
- **合併**：以 ffmpeg 合併為 mp4 / m4a（不重新編碼）

## 安裝

### 方式一：執行檔（Windows）

從 [Releases](../../releases) 下載 `StreamDownloader.exe`。已內嵌 Python 與 ffmpeg，不需另外安裝。

- 需要瀏覽器的功能（網站自動登入、瀏覽器監控）需安裝 Google Chrome
- 執行檔未簽章，第一次執行時 SmartScreen 可能警告，點「其他資訊」→「仍要執行」

### 方式二：pip 安裝（命令列與 Python 套件）

需要 Python 3.11 以上與 ffmpeg（加入 PATH，或以 `--ffmpeg` 指定）。

```bash
# 基本：直接下載串流網址（HLS / DASH / 一般檔案）
pip install "streamdl @ git+https://github.com/mm10079/StreamDownloader"

# 加上瀏覽器功能：網站自動登入、瀏覽器監控、--fetcher browser
pip install "streamdl[browser] @ git+https://github.com/mm10079/StreamDownloader"
```

安裝後可使用 `streamdl` 指令，或 `python -m streamdl`。

## 使用方式

### 直接雙擊執行檔

會詢問網址；直接按 Enter 則開啟瀏覽器，自行前往播放頁面。結束後按 Enter 關閉視窗。

### 命令列

```bash
# HLS / DASH 串流網址
StreamDownloader.exe "https://example.com/live/master.m3u8" -o downloads -t 標題
StreamDownloader.exe "https://example.com/stream/manifest.mpd" -o downloads

# 需要 Referer / Cookies 的串流
StreamDownloader.exe "https://example.com/index.m3u8" --referer "https://example.com/" --cookies cookies.txt

# 直播：一併回溯下載較早的片段
StreamDownloader.exe "https://example.com/live/master.m3u8" --backfill

# 一般網頁：開啟瀏覽器監控串流
StreamDownloader.exe "https://example.com/watch/123"
```

pip 安裝後把 `StreamDownloader.exe` 換成 `streamdl`；從原始碼執行則換成 `python -m streamdl`。

### 停止與續傳

- 第一次 **Ctrl+C**：停止追蹤直播的新片段，等待進行中的下載完成後照常合併
- 第二次 **Ctrl+C**：立即中斷
- 中斷或有片段失敗時，重新執行相同指令即可從進度接續
- 全部任務結束後，若還有開啟的瀏覽器，會保持開啟並等待按 Enter 才關閉，方便繼續觀看直播；`--no-keep-browser` 則直接關閉

### 網站登入

輸入支援的網站網址時會自動登入。帳密依序取自 `--account` / `--password`、環境變數 `STREAMDL_ACCOUNT` / `STREAMDL_PASSWORD`，都沒有時於終端機詢問；帳號直接按 Enter，或自動登入失敗時，改為在開啟的瀏覽器中手動登入。建議用環境變數，避免密碼出現在指令與 `.cmd` 檔中：

```powershell
# PowerShell
$env:STREAMDL_ACCOUNT="信箱"
$env:STREAMDL_PASSWORD="密碼"
```

```bat
:: 命令提示字元 / .cmd
set STREAMDL_ACCOUNT=信箱
set STREAMDL_PASSWORD=密碼
```

### 瀏覽器監控

1. 開啟瀏覽器後自行登入、前往播放頁面並開始播放。
2. 偵測到 m3u8 / mpd 時會在終端機詢問「下載這個串流？」。
3. 若前往的是支援的網站，會自動交給該網站的流程處理（含自動登入）。
4. 下載期間請保持瀏覽器開啟。

### 固定瀏覽器設定檔（`--chrome-profile`）

預設每次開啟的瀏覽器都是全新的設定檔。指定固定的設定檔資料夾後，**登入狀態與自行安裝的擴充功能（例如 VPN）都會保留**：

```bash
StreamDownloader.exe "網址" --chrome-profile "%LOCALAPPDATA%\StreamDownloader\chrome"
```

第一次使用時在開啟的瀏覽器中安裝需要的擴充功能，之後每次執行都會沿用。同一個設定檔同時只能由一個瀏覽器使用，執行前請先關閉以該設定檔開啟的 Chrome。

### 加密內容

一般的 HLS 加密與 DASH ClearKey 會自動處理。持有金鑰時以 `--key` 提供：

```bash
StreamDownloader.exe "https://example.com/manifest.mpd" --key KID:KEY
```

- 格式為 `KID:KEY`（32 位 hex），多組以逗號分隔；只有一組時可只填 `KEY`
- 也可直接填金鑰檔路徑，例如 `--key keys.txt`（每行一組）
- 不確定哪一組正確時可全部列出，會自動選出正確的一組
- Widevine / PlayReady / FairPlay 等 DRM 在沒有金鑰時無法下載

### 播放下載中的片段（`tools/play_m3u8.bat`）

把 `backup/標題/` 中的 `media.m3u8` 拖曳到 `tools/play_m3u8.bat` 上，即以 ffplay 播放（需 ffplay 在 PATH 或放在同資料夾）。加密片段與分軌的音訊會一併處理。

## 參數

| 參數 | 預設 | 說明 |
|---|---|---|
| `url` | | 串流或網站網址；省略則詢問，直接 Enter 開啟瀏覽器 |
| `-t`, `--title` | media | 無法從網站取得標題時使用的檔名 |
| `-o`, `--output` | downloads | 輸出資料夾 |
| `-q`, `--quality` | 0 | 畫質序號，0 為最高 |
| `--referer` | | Referer 標頭（直接下載串流網址時使用） |
| `--user-agent` | Chrome UA | User-Agent（使用瀏覽器時以瀏覽器實際值為準） |
| `--cookies` | | cookies 檔案（Netscape 格式）或 `a=1; b=2` 字串 |
| `--proxy` | | 代理伺服器，例如 `http://127.0.0.1:8080` |
| `-f`, `--fetcher` | httpx | 下載工具：`httpx` / `curl` / `browser` / `aria2` |
| `--concurrency` | 8 | 全域同時下載數 |
| `--per-host` | 6 | 單一主機同時下載數 |
| `--retries` | 5 | 每個片段的重試次數 |
| `--backfill` / `--no-backfill` | 關 | 回溯下載播放清單以外、伺服器上仍存在的較早片段（直播只列出最近片段時有用；部分網站不支援） |
| `--backfill-distance` | 10000 | 回溯時每輪往回探測的距離 |
| `--decrypt` / `--no-decrypt` | 關 | 下載中同步解密 HLS AES-128 片段 |
| `--key` | | 解密金鑰 `KID:KEY` 或 `KEY`，或金鑰檔路徑；多組以逗號分隔 |
| `--merge` / `--no-merge` | 開 | 完成後以 ffmpeg 合併 |
| `--ffmpeg` | ffmpeg | ffmpeg 路徑（exe 版已內嵌） |
| `--live-idle-limit` | 10 | 直播連續幾次沒有新片段就視為結束 |
| `--live-error-limit` | 10 | 連續幾次讀取播放清單失敗就視為結束 |
| `--account` / `--password` | 環境變數 | 支援的網站自動登入用的帳密 |
| `--media` / `--no-media` | 開 | 下載影音串流 |
| `--attachment` / `--no-attachment` | 開 | 下載附件 |
| `--skip` | | 略過的網址或 ID，以逗號分隔 |
| `--wait` / `--no-wait` | 開 | 直播尚未開始時等待開播 |
| `--browser` | auto | `auto` 需要時才開、`always`、`never` 不開瀏覽器 |
| `--chrome-path` | 自動尋找 | Chrome 執行檔路徑 |
| `--chrome-profile` | | 固定的瀏覽器設定檔資料夾（保留登入與擴充功能） |
| `--headless` / `--no-headless` | 關 | 瀏覽器無頭模式（需要手動登入或操作時無效） |
| `--keep-browser` / `--no-keep-browser` | 開 | 全部任務結束後保持瀏覽器開啟，按 Enter 才關閉 |
| `--aria2-rpc` / `--aria2-secret` | | aria2 RPC 位址與密鑰（需先啟動 `aria2c --enable-rpc`） |

## 輸出資料夾結構

```
downloads/
├── 標題.mp4                    合併完成的檔案
└── backup/標題/                下載的片段與續傳進度
```

確認合併結果無誤後，`backup/` 可以刪除。

<details>
<summary>backup 資料夾詳細內容</summary>

```
backup/標題/
├── playlists/              原始 m3u8 / mpd 備份
├── fragments/              HLS 片段、金鑰、media.m3u8（可手動以 ffmpeg 合併）
├── decrypted/              --decrypt 時的解密片段
├── video/ audio/           分軌下載時各軌的片段（結構同上）
├── play.m3u8               以 tools/play_m3u8.bat 播放分軌時產生
└── store.json              片段下載狀態（續傳用）
```

</details>

## 限制

- Widevine / PlayReady / FairPlay 等 DRM 沒有金鑰時無法下載。
- HLS 獨立音軌只下載一條（優先預設音軌），目前無法指定語言。
- DASH 影像與音訊兩軌起點不同時，合併後可能影音不同步。
- DASH 多 Period（例如插入廣告）只下載第一個 Period。
- 字幕軌目前略過。
- aria2 下載方式尚未實測。

---

## 在 Python 程式中使用

```python
import streamdl

result = streamdl.download(
    "https://example.com/live/master.m3u8",
    output="downloads",
    title="標題",
    quality=0,                   # 所有命令列參數都可使用（底線取代連字號）
)
if result.ok:
    print(result.files)          # 產生的檔案（串流輸出與附件）
else:
    print(result.error)          # 解析階段的錯誤
    for s in result.streams:
        print(s.title, s.ok, s.error, len(s.failed))
```

非同步程式（例如 FastAPI、discord.py）請使用 `await streamdl.adownload(...)`。

**進度**：傳入 `progress_hook`，會收到 dict 事件：

```python
def hook(event):
    if event["type"] == "progress":
        # status：started / running / finished / error
        print(event["description"], event["completed"], event["total"], event["status"])
    else:                        # {"type": "log", "level": "INFO", "message": "..."}
        print(event["level"], event["message"])

streamdl.download(url, progress_hook=hook)
```

**當作套件使用時的行為**

| 項目 | 行為 |
|---|---|
| 終端機輸出 | 不顯示 Rich 畫面；訊息寫入 `logging`（logger 名稱 `streamdl`），未設定 logging 時完全安靜 |
| 互動詢問 | 不詢問（帳密、標題等）；需要時以參數傳入，例如 `account=…`、`password=…`。可用 `interactive=True` 開啟 |
| 瀏覽器 | 下載完成後立即關閉（`interactive=True` 時才會等待 Enter） |
| 停止 | 傳入 `stop=asyncio.Event()`，設定後停止追蹤直播並完成進行中的下載 |
| 參數檢查 | 參數名稱拼錯會拋出 `pydantic.ValidationError` |
| 並行 | 可同時執行多個 `adownload`，各自的 `progress_hook` 互不干擾 |

回傳的 `DownloadResult`：

| 欄位 | 說明 |
|---|---|
| `ok` | 沒有錯誤、有內容，且所有串流與附件都成功 |
| `files` | 所有成功產生的檔案 |
| `title` / `output_dir` / `extractor` | 標題、輸出資料夾、使用的提取器 |
| `error` | 解析階段錯誤（不支援的網址、登入失敗等） |
| `streams` | 每條串流：`title`、`kind`、`url`、`ok`、`output`（最終檔案）、`backup`（片段資料夾）、`failed`（失敗片段網址）、`error` |
| `attachments` | 每個附件：`path`、`ok`、`url` |

## 開發

```bash
pip install -e ".[browser,dev]"
python -m pytest            # 測試（不需要網路、Chrome 或 ffmpeg）
python -m streamdl --help   # 從原始碼執行
```

打包執行檔（Windows）：

```bash
pip install pyinstaller
python -m PyInstaller StreamDownloader.spec --noconfirm
```

會自動尋找 ffmpeg 並內嵌；可用環境變數 `FFMPEG_BIN` 指定。產出位於 `dist/StreamDownloader.exe`。

架構說明（各層分工、新增網站提取器的方式）請見 [docs/架構設計.md](docs/架構設計.md)。
