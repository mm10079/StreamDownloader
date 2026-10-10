# StreamDownloader

串流下載器：支援 **HLS（m3u8）** 與 **DASH（mpd）**，可下載直播與存檔，並能**回溯**播放清單以外、伺服器上仍存在的較早片段。支援的網站可自動登入並找出串流，其他網站可用瀏覽器監控自動找出串流。

## 功能

| 功能 | 說明 |
|---|---|
| HLS | 主播放清單畫質選擇、獨立音訊軌（EXT-X-MEDIA）自動一併下載並合併（分存於 `backup/{標題}/video`、`audio`）、AES-128 / SAMPLE-AES 解密（含未提供 IV 時依序號推算）、EXT-X-MAP、BYTERANGE |
| DASH | SegmentTemplate（`$Number$` / `$Time$` / SegmentTimeline）、SegmentList、SegmentBase；影像與音訊分軌下載後合併 |
| 直播 | 持續監控播放清單 / MPD，直到直播結束或連續沒有新片段 |
| 回溯（自動探測） | 由片段網址推出模板，探測播放清單以外的較早片段（詳見下方） |
| CENC 解密 | DASH 加密內容可用 `--key` 提供金鑰，或自動向 ClearKey 授權伺服器取得（詳見「加密內容與 DRM」） |
| 斷點續傳 | 每條串流的片段狀態存在 `store.json`，重新執行相同指令即可接續 |
| 瀏覽器監控 | 沒有網址或網站不支援時，開啟瀏覽器偵測頁面中的 m3u8 / mpd，逐一詢問是否下載 |
| 下載工具 | httpx（預設）、curl、瀏覽器內下載、aria2 |
| 合併 | ffmpeg 合併為 mp4 / m4a（不重新編碼） |

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

開發時從原始碼執行：

```bash
pip install -r requirements.txt
python -m streamdl --help
```

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

# 一般網頁：開啟瀏覽器監控串流
StreamDownloader.exe "https://example.com/watch/123"

```

pip 安裝後把 `StreamDownloader.exe` 換成 `streamdl`；從原始碼執行則換成 `python -m streamdl`。

### 直接播放下載中的片段（`tools/play_m3u8.bat`）

把 `backup/標題/` 中的 `media.m3u8` 拖曳到 `tools/play_m3u8.bat` 上，即以 ffplay 播放（需 ffplay 在 PATH 或放在同資料夾）：

- 會一併讀取本地金鑰檔（`.key`）解密播放；一般播放器會擋下非影音副檔名的金鑰檔，也多半不支援 SAMPLE-AES
- 拖入分軌下載的 `video/fragments/media.m3u8` 時，會自動產生 `play.m3u8` 把 `audio/` 音軌一起播放
- 本地 `media.m3u8` 保留原始的 `#EXT-X-PROGRAM-DATE-TIME`；片段檔名為原始的媒體序號（補零），可據此手動對齊兩軌

### 停止與續傳

- 全部任務結束後，若還有開啟的瀏覽器（網站登入、瀏覽器監控），會保持開啟並等待按 Enter 才關閉，方便繼續觀看直播到結束；`--no-keep-browser` 則直接關閉。

- 第一次 **Ctrl+C**：停止追蹤直播的新片段，等待進行中的下載完成後照常合併
- 第二次 **Ctrl+C**：立即中斷
- 中斷或有片段失敗時，重新執行相同指令即可從進度接續

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

## 支援的網站與模式

提取器依下列順序比對網址：

1. **網站專用**：支援的網站會自動登入（見參數 `--account` / `--password`）並取得串流與附件
2. **直接串流網址**：網址為 `.m3u8`、`.mpd` 或一般媒體檔（mp4 / mp3 等）
3. **瀏覽器監控**：以上都不符合，或沒有輸入網址

### 固定瀏覽器設定檔（`--chrome-profile`）

預設每次開啟的瀏覽器都是全新的暫存設定檔。指定固定的設定檔資料夾後，**登入狀態與自行安裝的擴充功能（例如 VPN）都會保留**：

```bash
StreamDownloader.exe "網址" --chrome-profile "%LOCALAPPDATA%\StreamDownloader\chrome"
```

1. 第一次使用時，在開啟的瀏覽器中到 Chrome 線上應用程式商店安裝需要的擴充功能並完成設定。
2. 之後每次執行都會沿用。
3. 同一個設定檔同時只能由一個瀏覽器使用：執行前請關閉以該設定檔開啟的 Chrome；同時開啟多個瀏覽器時，只有第一個使用固定設定檔。

> 選擇 VPN 擴充功能時請留意隱私：部分免費 VPN 擴充功能（例如 Urban VPN Proxy）曾被揭露會蒐集並轉售使用者的瀏覽資料，而此瀏覽器會登入你的購票帳號。

### 瀏覽器監控

1. 開啟瀏覽器後自行登入、前往播放頁面並開始播放。
2. 偵測到 m3u8 / mpd 時會在終端機詢問「下載這個串流？」。
3. 若前往的是支援的網站，會自動交給該網站的流程處理（含自動登入）。
4. 下載期間請保持瀏覽器開啟（cookies 由瀏覽器持續同步）。

## 回溯（自動探測較早片段）

許多直播的播放清單只列出最近幾分鐘的片段，但伺服器上更早的片段仍可下載。回溯會推出片段網址的規則，主動探測更早的片段：

| 類型 | 推出模板的方式 | 探測方式 |
|---|---|---|
| HLS，序號連續 | 比對兩個片段網址中變動的數字，例如 `index_01669.ts` → `index_{num}.ts` | 分段二分搜尋，找出最早的有效序號 |
| HLS，時間戳命名 | 同上，間距為時間戳差值 | 先試常見間距，再於範圍內並行掃描，並學習新的間距 |
| DASH `$Number$` | 直接使用 MPD 的 SegmentTemplate | 二分搜尋 |
| DASH `$Time$` | 直接使用 MPD 的 SegmentTemplate | 以 SegmentTimeline 中出現過的片段長度往前推，最後探測時間起點 |

- 預設開啟；`--no-backfill` 關閉。
- 片段網址中帶有每段不同的簽章時無法推出模板，會自動略過。
- 部分網站預設關閉回溯。

## 加密內容與 DRM

| 類型 | 能否下載 | 方式 |
|---|---|---|
| HLS AES-128 / SAMPLE-AES（金鑰為一般網址） | ✅ | 自動下載金鑰並解密 |
| HLS SAMPLE-AES fMP4（cbcs），且你持有金鑰 | ✅ | `--key KID:KEY`；影像與音訊不同 KID 時依 KID 自動對應，合併時由 ffmpeg 解密 |
| DASH ClearKey | ✅ | 自動向 MPD 中的授權伺服器取得明文金鑰 |
| DASH / fMP4 CENC，且你持有金鑰 | ✅ | `--key KID:KEY`（支援 cenc、cbcs） |
| Widevine / PlayReady / FairPlay | 沒有金鑰 ❌ | 沒有 `--key` 時偵測到即回報，不會下載；以 `--key` 提供金鑰即可照常下載解密 |

- **為什麼 Widevine 等無法下載**：這類 DRM 的金鑰只會交給瀏覽器或裝置內經過授權的解密模組（CDM），網頁與下載器都拿不到。取得金鑰需要規避技術保護措施，在台灣（著作權法第 80 條之 2）、日本、美國（DMCA §1201）皆屬違法，本工具不支援。
- **`--key` 格式**：`KID:KEY`（32 位 hex，KID 可含連字號），多組以逗號分隔；只有一組且不知道 KID 時可只填 `KEY`。僅適用於你合法持有金鑰的內容（例如自己的影片、服務方提供的金鑰）。
- **金鑰檔**：和 `--cookies` 一樣，`--key` 可直接填檔案路徑，例如 `--key keys.txt`。檔案中每行一組，`KID:KEY`、只有 `KEY`、UUID 形式的 KID、base64、其他工具輸出的整行（如 `--key KID:KEY`）、ClearKey JSON、16 bytes 二進位的 `.key` 檔都能辨識；`#` 開頭的行與沒有金鑰的文字會略過。也可與金鑰字串混用：`--key "keys.txt,KID:KEY"`。
- **多組金鑰自動試解**：不確定哪一組正確時可全部列出，會以實際片段試解選出正確的一組（log 顯示「金鑰試解：第 N 組正確」）；都試不出來時使用第一組，照常下載解密。只有一組時直接使用，不試解。
  - HLS：fMP4 片段（含 SAMPLE-AES）先讀 init 標示的 KID，選用 KID 相同的金鑰（影像與音訊常是不同 KID）；對應不上時再試解——AES-128 檢查 PKCS7 填充與 TS / fMP4 等格式特徵，SAMPLE-AES fMP4 交給 ffmpeg 試解碼
  - DASH：KID 對應得上就直接使用；對應不上的（未指定 KID，或 MPD 沒標示 KID）於下載完成後以 init + 第一個片段解密，交給 ffmpeg 試解碼選出
- 解密使用內建實作（cenc / cbcs）；若已安裝 Bento4 的 `mp4decrypt` 會優先使用。
- 解密後會以 ffmpeg 試解前幾秒，若無法正常解碼會提示「金鑰可能錯誤」（AES-CTR 無法從密文判斷金鑰是否正確）。

```bash
StreamDownloader.exe "https://example.com/manifest.mpd" --key eb676abbcb345e96bbcf616630f1a3da:100b6c20940f779a4589152b57d2dacb
```

## 輸出資料夾結構

```
downloads/
├── 標題.mp4                    合併完成的檔案
└── backup/標題/
    ├── playlists/              原始 m3u8 / mpd 備份
    ├── fragments/              HLS 片段、金鑰、media.m3u8（可手動以 ffmpeg 合併）
    ├── decrypted/              --decrypt 時的解密片段
    ├── video/ audio/           DASH 各軌的片段、串接後的 video.mp4 / audio.mp4（加密時另有 *.decrypted.mp4）
    └── store.json              片段下載狀態（續傳用）
```

HLS 有獨立音訊軌（`EXT-X-MEDIA`）時，影像與音訊分開存放，最上層只有主播放清單：

```
backup/標題/
├── playlists/                  主播放清單備份
├── video/                      影像：playlists/、fragments/（含 media.m3u8）、decrypted/、store.json
├── audio/                      音訊：結構同上
└── play.m3u8                   以 tools/play_m3u8.bat 播放時產生（影像 + 音訊）
```

確認合併結果無誤後，`backup/` 可以刪除。

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
| `--backfill` / `--no-backfill` | 開 | 回溯較早片段 |
| `--backfill-distance` | 10000 | 連續序號時每輪往回探測的距離 |
| `--decrypt` / `--no-decrypt` | 關 | 下載中同步解密 HLS AES-128 片段 |
| `--key` | | 解密金鑰 `KID:KEY` 或 `KEY`，或金鑰檔路徑（每行一組）；多組以逗號分隔，會依 KID 對應或試解選出正確的一組（僅限合法持有的金鑰） |
| `--merge` / `--no-merge` | 開 | 完成後以 ffmpeg 合併 |
| `--ffmpeg` | ffmpeg | ffmpeg 路徑（exe 版已內嵌） |
| `--live-idle-limit` | 10 | 直播連續幾次沒有新片段就視為結束 |
| `--live-error-limit` | 10 | 連續幾次讀取播放清單失敗就視為結束 |
| `--account` / `--password` | 環境變數 | 支援的網站自動登入用的帳密，見下方說明 |
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

**網站登入（`--account` / `--password`）**：輸入支援的網站網址時會自動登入。帳密依序取自參數、環境變數 `STREAMDL_ACCOUNT` / `STREAMDL_PASSWORD`，都沒有時於終端機詢問；帳號直接按 Enter，或自動登入失敗時，改為在開啟的瀏覽器中手動登入，登入後自動繼續。建議用環境變數，避免密碼出現在指令與 `.cmd` 檔中：

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

## 限制

- Widevine / PlayReady / FairPlay 等 DRM 不支援（見「加密內容與 DRM」）。
- CENC 解密不支援以 `saio` 指向 mdat 的輔助資訊格式，以及 `tfhd` 帶絕對 `base_data_offset` 的檔案（可安裝 `mp4decrypt` 處理）。
- HLS 獨立音軌只下載一條（優先 `DEFAULT=YES`，其次 `AUTOSELECT=YES`），目前無法指定語言。
- DASH 影像與音訊兩軌的起點不同時（例如回溯找到的範圍不同），合併後可能影音不同步。HLS 獨立音軌會依 `EXT-X-PROGRAM-DATE-TIME` 或共同的片段序號自動對齊。
- DASH 多 Period（例如插入廣告）只下載第一個 Period（直播為目前的 Period）。
- 字幕軌目前略過。
- aria2 下載方式已實作但尚未實測。

## 開發

```bash
pip install -e ".[browser,dev]"
python -m pytest            # 測試（不需要網路、Chrome 或 ffmpeg）
```

打包執行檔（Windows）：

```bash
pip install pyinstaller
python -m PyInstaller StreamDownloader.spec --noconfirm
```

會自動尋找 ffmpeg 並內嵌；可用環境變數 `FFMPEG_BIN` 指定。產出位於 `dist/StreamDownloader.exe`。

架構說明（Session / Fetcher / Protocol / Backfill 分層、新增網站提取器的方式）請見 [docs/架構設計.md](docs/架構設計.md)。
