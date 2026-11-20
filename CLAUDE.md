# StreamDownloader（streamdl）專案說明

給 Claude Code 與接手開發者的工作階段交接文件。最後更新：2026-11-20。
**一律以繁體中文回覆使用者。**

## 專案概要

- HLS（m3u8）/ DASH（mpd）串流下載器：直播監控、斷點續傳、回溯較早片段、AES-128 / CENC 解密、ffmpeg 合併。
- 網站適配器：ZAN-LIVE、SINGULAR LIVE、Twitter（X）Space；沒有適配器時以「直接網址」或「瀏覽器監控」處理。
- 形式：CLI（`streamdl` / `python -m streamdl`）、Windows exe（內嵌 ffmpeg）、Python 套件（`streamdl.download()`）。
- Repo：`github.com/mm10079/StreamDownloader`（私有）。目前版本 **0.5.3**（`streamdl/__init__.py` 是唯一的版本來源，`setup.py` 會讀取）。
- 舊版專案（僅供參考，不再開發）：`M:\自製資料\開發中\專案進行中\m3u8下載器`。

## 新設備環境設定

```bash
git clone https://github.com/mm10079/StreamDownloader
pip install -e ".[browser,dev]"     # browser = undetected-chromedriver；dev = pytest、pyinstaller
python -m pytest                     # 不需要網路、Chrome 或 ffmpeg
```

- 需要：Python 3.11+、ffmpeg（PATH 或 `--ffmpeg`）、Google Chrome（瀏覽器相關功能）。
- 專案若放在網路磁碟，git 會出現 dubious ownership：指令加 `git -c safe.directory=*`，或 `git config --global --add safe.directory '*'`。
- GitHub CLI 在 Windows 的 Git Bash 可能不在 PATH：`"C:\Program Files\GitHub CLI\gh.exe"`。
- ZAN-LIVE / SINGULAR LIVE 帳密：環境變數 `STREAMDL_ACCOUNT` / `STREAMDL_PASSWORD`（PowerShell 用 `$env:X="..."`，不是 `set`），或執行時輸入。**不要把帳密寫進任何檔案或 commit。**

## 架構（`streamdl/`）

```
cli.py / api.py → core/pipeline.py：find_extractor → extract() → MediaJob → protocol.run() → ffmpeg 合併
extractor/      網站適配器（adapters/ 下的 InfoExtractor 子類別會自動載入，priority 小者先比對）
session/        Session：唯一的 CookieJar + headers + proxy；refresh() single-flight；瀏覽器可為 cookie 權威
fetcher/        把一個網址抓成一個檔案（httpx 預設 / curl / browser / aria2），並行限制在這層
protocol/       hls/、dash/、file/：解析清單、片段、加密、直播輪詢、本地清單
backfill/       回溯：由片段網址推出 {num} 模板並探測更早片段（預設關閉，--backfill 開啟）
postprocess/    ffmpeg 合併 / 封裝（含進度）、CENC 解密（cenc / cbcs，內建實作）
core/store.py   片段狀態 store.json（續傳）；core/result.py 回傳給 API 的結果
driver/         undetected-chromedriver（uc.py）、CDP 網路監聽（cdp_watch.py）、設定檔工具（profile.py）
utils/          console（Rich）、log（訊息 / 詢問）、reporters（logging / progress_hook）、paths
```

詳細設計見 `docs/架構設計.md`；使用說明見 `README.md`。

### 提取器比對順序
1. 網站專用（priority 10）：`singular-live`、`twitter-space`、`zan-live`
2. `direct`（1000）：網址含 `.m3u8` / `.mpd` / 媒體副檔名
3. `browser`（2000）：開瀏覽器，監聽頁面發出的 m3u8 / mpd 請求並詢問是否下載；進入支援的網站頁面時交給該網站的提取器

## 重要設計原則與踩過的坑

- **cookies 只有一份**：httpx client 共用 Session 的 CookieJar（不是複製）。外部工具需要時以 `headers_for(url)` 即時產生。瀏覽器為權威時只會「瀏覽器 → jar」。
- **ZAN-LIVE**：每個視角各自登入、各開一個瀏覽器，**由瀏覽器進入直播間後**才讀取 cookies 與串流網址；httpx 從不讀取直播間頁面（進出直播間會改變伺服器端播放狀態，下載會被踢）。下載期間每 10 秒從瀏覽器同步 cookies。
- **SINGULAR LIVE**：只處理已購買票券（`/api/mypage/available-tickets`）；直播已結束改下載關聯存檔；日本 IP 限定時提示開 VPN；登入頁有 Cloudflare Turnstile，按鈕可點擊後才點（等待使用者的瀏覽器自然通過或手動勾選）；ULIZA 播放器參數只對已購票券讀取。
- **Twitter Space**：片段位於 master playlist 的路徑下（以 media playlist 路徑解析會 HTTP 400），HLS 的替代路徑探測會同時嘗試兩者；資料夾 `{播出日期} - Twitter Space #{標題}`，日期取自 `EXT-X-PROGRAM-DATE-TIME`。
- **瀏覽器監控**：performance log 看不到跨網站 iframe 與 Web Worker 的請求，因此另以 CDP 自動附加所有目標監聽（`driver/browser/cdp_watch.py`）。
- **當作套件使用時保持安靜**：import 不註冊 Rich；輸出走 contextvar `CURRENT_PUSHER` → CLI 的 RichPusher → logging（logger `streamdl`，掛 NullHandler）。API 預設不互動（`log.INTERACTIVE`）。
- **回溯與續傳**：關閉回溯時會移除上次回溯加入的片段；續傳時沿用上次的排序鍵模式，避免同一片段存兩份。
- **檔案寫入**：片段先寫 `.part` 再改名，存在的檔案必定完整。
- **Windows 注意**：網址含 `&` 必須加引號；PowerShell 5.1 傳遞含雙引號的長字串會被切碎，發布說明用 `gh release create --notes-file`；stdin 導向 NUL 時 `isatty()` 仍為 True（`log.ask` 已處理 EOF）。

## 範圍界線（不要實作）

- 不支援 Widevine / PlayReady / FairPlay 等 DRM 的解密或金鑰取得；偵測到時停止並說明。`--key` 僅用於使用者合法持有金鑰的內容（DASH CENC、ClearKey）。
- 不開發、不整合用來繞過 Cloudflare 或其他機器人偵測、人機驗證的功能；驗證一律由使用者在瀏覽器中完成。
- 不做「對任意網站自動填入帳密」：帳密可能被送到錯誤網站。建議以 `--chrome-profile` 搭配 Chrome 記住密碼。
- 不預設安裝第三方 VPN 擴充功能（隱私疑慮；Chrome 137+ 也不支援以參數載入擴充功能）。

## 測試與驗證

- `python -m pytest`：單元與端對端測試（本機模擬伺服器、假瀏覽器、CENC 測試加密器 `tests/unit/cenc_fixture.py`）。2026-11-20 為 **141 passed**。
- 真實檔案驗證：以 ffmpeg 產生測試資料（`testsrc` + `sine`，`-f hls` 加 `-hls_key_info_file`、`-f dash -use_template 1 -use_timeline 1`），以 cenc_fixture 加密成 cenc / cbcs 版本，再用本機 HTTP 伺服器跑 CLI 或 exe，以 ffprobe 檢查時長、framemd5 比對。
- 驗證 exe 時把 ffmpeg 從 PATH 移除（`PATH=C:\Windows\System32;C:\Windows`），確認使用的是內嵌版。

## 打包與發布

```bash
python -m PyInstaller StreamDownloader.spec --noconfirm   # → dist/StreamDownloader.exe（自動內嵌 ffmpeg，可用 FFMPEG_BIN 指定）
python -m pip wheel . --no-deps -w dist                   # → dist/streamdl-<版本>-py3-none-any.whl
```

流程：改 `streamdl/__init__.py` 的 `__version__` → commit「Release vX.Y.Z」→ push → 打包 → 驗證 exe → `git tag -a vX.Y.Z` → push tag → `gh release create vX.Y.Z dist/StreamDownloader.exe dist/streamdl-X.Y.Z-py3-none-any.whl --notes-file 說明.md`。

開發習慣：較大的功能開 `feature/*` 分支 → PR → merge；小修正與版本號可直接在 main。

## 目前狀態（2026-11-20）

已發布 v0.5.3。相較 v0.5.2（瀏覽器監控可擷取跨網站 iframe / Web Worker 的請求），v0.5.3 包含：
- 固定瀏覽器設定檔預設開啟（`%LOCALAPPDATA%\StreamDownloader\chrome`；`--chrome-profile none` 改用暫存設定檔），新增 `driver/browser/profile.py`
- 同時需要多個瀏覽器時，後續瀏覽器使用設定檔副本（含擴充功能，登入 cookies 以 CDP 複製）
- SINGULAR LIVE：多個視角各用一個瀏覽器同時進入直播間；視窗標題標記視角；區域限制時以瀏覽器網路偵測 VPN 是否已連上並自動重新進入
- 瀏覽器監控進入 SINGULAR LIVE 官網任何頁面時先自動登入（`prepare_browser`）
- 對應的 README、架構文件與測試（`tests/unit/test_chrome_profile.py` 等）
- 修正：PATH 中沒有 PowerShell 時讀不到 Chrome 版本，導致下載到不相符的 ChromeDriver；改為先讀 chrome.exe 旁的版本資料夾，再以絕對路徑呼叫 PowerShell

## 已知限制 / 可能的後續工作

- DASH 多 Period 只下載第一個（直播為目前的 Period）；字幕軌略過。
- aria2 下載方式已實作但未實測。
- 播放清單回傳 403 且網址疑似被 `&` 截斷時，可提示使用者加引號（尚未實作）。
- HLS 的 master 階段可提早依 `EXT-X-SESSION-KEY` 回報 DRM（目前在 media playlist 階段判斷）。
