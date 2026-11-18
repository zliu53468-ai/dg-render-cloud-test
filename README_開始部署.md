# DG Render 雲端 Chromium + V6.4 可見畫面辨識 — V1 測試版

> 狀態：**程式已完成、離線測試通過；尚未部署到你的 Render，也尚未驗證 DG 在 Render 真的能播放直播畫面。**
> 只處理使用者授權可見的畫面；不存取遊戲商 API/密鑰/封包，不自動登入、不解 CAPTCHA、不刷假注單、不繞過 404/閒置登出。所有點數都是未驗證的 OCR 候選。

## 你要的架構

1. **Render Docker Web Service** 開啟真正的 Linux Chromium（Playwright），不需要你自己的 Windows 電腦開機。
2. 你透過受密碼保護的 `/operator` 頁面，手動進入有授權的網頁及 DG 房間。
3. 程式從**雲端 Chromium 的可見頁面截圖**（非 Windows `mss` 螢幕），並讓你框選比分／荷官桌號／倒數／珠盤路四個區域。
4. OCR 復用本地 V6.4 的 `dg_rb02_v6.py`、`dg_current_shoe_v6.py`、`score_engine_v3.py`。要有**連續有效桌號**，且畫面足以確定分靴狀態才會保存候選。
5. 預設將候選 CSV、JSONL、比分裁切證據存到容器 `/tmp/dg-cloud-observer`。**這是暫時儲存**；部署/重啟就可能遺失。需要長久儲存再綁 Render Disk 或既有後端資料庫/API。

**本套件不會連到 BGS-COPY.1，完全獨立；也不會修改目前可用的本機 V6.4。**

## Render 限制（已核對官方文件）

| 項目 | 說明 |
| --- | --- |
| Docker、Chromium | Render 支援 Docker；Playwright 發布 Linux Chromium Docker 映像，可當基底 |
| Web Service | 必須綁 `0.0.0.0:$PORT`；本專案已設計 |
| 付費服務 | 付費的運算實例不會因閒置流量 spin down，但**重新部署仍會重啟容器** |
| 既有 Render 收費 | **帳號已付費不代表再開一台新服務就免費**；新建服務／加磁碟可能加收費用 |
| 512MB | 可能不足以同時執行 Chromium、遊戲影片、OCR；先看 Render Metrics，再決定是否增加規格 |
| DG 登入過期 | 雲端不休眠≠DG 工作階段永遠有效；網站需要人類驗證/重新登入時必須人工處理 |
| 遊戲影片 | H.264/WebRTC/WebGL/DRM 及網站反自動化策略可能無法在 Linux Chromium 正常播放；**沒有正式實測不能保證** |
| 資料持久性 | Render Web Service 預設檔案系統為 ephemeral；請勿把它當唯一資料庫 |

官方參考：
- https://render.com/docs/web-services
- https://render.com/docs/docker
- https://render.com/docs/faq
- https://render.com/docs/disks
- https://playwright.dev/docs/docker

## 原本專案調查

過去的 DG 雲端 Playwright 試驗分支：
- `https://github.com/zliu53468-ai/BGS-COPY.1/tree/dg-assisted-cloud-ocr-20261117`
- 其中 `dg_cloud_operator.py` 是人工操作式雲端視窗，原來轉送到 BGS OCR 接口。
- 現有 BGS `render.yaml` 的正式服務是 Python/LINE 服務（不是你本機的 Node `bofu58-game-integration`）。

**為避免誤改你的正式 BGS/LINE 功能，本套件先不修改該儲存庫、不合併 main、不直接重部署。**
你 Windows 本機 Node 專案的 `server.mjs`/`Dockerfile`/`render.yaml` 實際內容還沒有提供完整原始檔，因此目前無法安全地把本套件合併進你「已付費的那個特定服務」；這一步需要確認你付費 Render 服務連的是哪個 GitHub Repo / branch，以及部署設定。

## 第一步：把整個資料夾推到你自己的 GitHub 測試分支

例如把 `DG_Render_Cloud_Chromium_V1` 這個資料夾放在你**DG 專案** Git 倉庫根目錄，不要放到 BGS 正式 main 中。

不要上傳 `.env`、Cookie、登入 Token、會員資料、含私密網址的截圖。

## 第二步：在 Render 建立或配置雲端 Docker 服務

先到 Render Dashboard → 你的現有服務，記下：`Service name`、`Repository`、`Branch`、`Runtime`、`Plan`。不要直接按 Deploy 去覆蓋原本服務。

**安全的獨立測試方式（可能增加 Render 費用）：**

- New → Web Service → 連到包含本資料夾的 GitHub Repo，選測試分支
- Language / Runtime：**Docker**
- Root Directory：留空
- Dockerfile Path：`./DG_Render_Cloud_Chromium_V1/Dockerfile.dg-cloud`
- Docker Build Context：`./DG_Render_Cloud_Chromium_V1`
- Health Check：`/health`
- Instance Plan：先檢查現有規格，0.5CPU/512MB 可能會 OOM

也有 `render.cloud.example.yaml` 可參考，但**不要把它直接覆蓋現有 `render.yaml`**。它會建立新的 Web Service，有新增費用風險。

**若你要求完全不增加第二台服務的運算費，應先提供現有 Render 服務 GitHub repo 與 `Dockerfile`、`server.mjs`、`render.yaml`、`package.json`，再決定能否安全併入同一實例；不能在未看原碼的情況下硬塞入正式服務。**

## 第三步：Render Environment 設定

請在 Render Dashboard 設定（不要在聊天室貼密碼）：

| Key | 範例 / 說明 |
| --- | --- |
| `DG_CONTROL_TOKEN` | **自行產生至少 32 字元**的隨機管理金鑰，絕對不要寫入 GitHub |
| `DG_START_URL` | `https://dg66.net/Opengame/freegame/`（舊測試分支使用的公開試玩入口；現況是否可用須實測） |
| `DG_ALLOWED_HOSTS` | `dg66.net`；如已授權的頁面正常切換到其他網域，才加入其**主機名稱**（逗號分隔，**不要填整條帶 token 的網址**） |
| `DG_VIEWPORT_WIDTH` | `1600` |
| `DG_VIEWPORT_HEIGHT` | `900` |
| `DG_FRAME_INTERVAL_SECONDS` | `1.25`；若 CPU 不夠可調慢但可能漏抓短暫比分條 |
| `DG_PREVIEW_INTERVAL_SECONDS` | `2` |
| `DG_STORAGE_DIR` | 不設則暫時寫在 `/tmp/dg-cloud-observer`；綁 Render Disk 後可設 `/var/data/dg-cloud` |

此版本只允許到 `DG_START_URL` 指定的 HTTPS 公開起始頁，不接受帶查詢字串或已簽名 session URL 作為啟動值。

## 第四步：部署後本機操作

1. Render 部署成功，瀏覽 `https://你的服務.onrender.com/health`，應回 `{"ok":true,...}`。
2. 到 `https://你的服務.onrender.com/operator`。輸入 `DG_CONTROL_TOKEN`，按「連線狀態」。
3. 點「啟動雲端 Chromium」；確認**雲端截圖真實顯示**起始頁。若 Render 能開頁但影片黑屏，先停下 OCR，不應稱為接通。
4. 用操作台的「人工點擊」＋「在雲端輸入文字」合法進入遊戲（人工驗證碼需自行輸入；系統不會嘗試破解）。**不要讓 ChatGPT 取得帳密**。
5. 看到真實牌桌後，依序以滑鼠畫四個區域：`score`（閒點 VS 莊點比分條）、`header`（荷官＋桌號）、`timer`（綠色倒數圈）、`road`（六排珠盤路）。
6. 確認目前**實際桌號**與荷官、倒數確實可讀，再點「開始 V6.4 OCR」。
7. 對照 5–10 局真實最終點數、桌號及分靴；只有候選紀錄不等於正式結算。
8. 先測「同桌 30 分鐘不操作」；再測網站是否自然登出、返回大廳或 404。若出現這些狀況，**暫停 OCR 並由本人依網站正常流程重新登入**。

操作台是透過 HTTPS 將你人工輸入的內容送到**你自己的 Render 主機**，不會寫入本程式的 CSV/日誌；但你仍應確認服務安全、獨立管理金鑰並使用專用的測試帳號。預覽只在記憶體中保存最近一幀，遮蔽右上角常見帳號區；**仍可能有其他個人資訊，不要公開分享**。

## 故障判斷

- `/health` 正常、`browser: error`：Chromium 啟動失敗或記憶體不足，先看 Render Logs / Metrics。
- `page: host_not_allowed`：網站跳到另一個域名。確認是否為合法授權後，將**單純主機名稱**加到 `DG_ALLOWED_HOSTS`，不必貼含 Token 的網址。
- `page: http_error`：原始網站可能 404／401；程式不會重新登入或繞過限制。
- `table: null`：畫面還沒成功辨識當前桌號或校準偏移，不要把舊桌號當現在桌號。
- `state: TABLE_ID_UNCONFIRMED`：候選入庫暫停保護生效。
- `candidate_count` 不動：比分可能尚未出現／太短／OCR 不穩，不能單憑計數推論網站失效。
- `road_markers` 大幅減少：不能只因此認定已洗牌，V6.4 需更強的連續清空＋增長訊號。
- 雲端瀏覽器已開但畫面黑屏：很可能是影片/圖形串流/相容性問題，需要另做相容性調查；沒有替代資料來源不能推論結果。
- Render 重新部署：Chromium 會話與未持久化資料可能重置；需要人工重新進入遊戲。

## 本版本已完成的測試

- 原始 V6.4 `--self-test` 通過（RB02/RB03、20/22 秒、47→12 不誤判洗牌、模擬 RB03→RB05）。
- 9 項 Python 測試通過，包含遮蔽預覽、ROIs 裁切、真實 OCR 樣本與 HTTP 權限保護。
- JS 已通過 `node --check` 語法檢查。
- **尚未能在此環境實際啟動 Docker Chromium，也沒有使用你的 Render/GD 帳密，所以沒有宣稱 DG 雲端播放已驗證。**

測試方式：

```bash
python -m unittest discover -s tests -v
python dg_current_shoe_v6.py --self-test
```

## 日後把雲端候選接進你原本的 Render Node 後端

目前雲端程式**不會**送到 BGS 或任何第三方端點；保留獨立儲存，優先確認雲端畫面能播放/辨識。

要接進 `bofu58-game-integration` 原本的 `server.mjs`，應由你提供真實原始碼後，為它新增**受金鑰保護**的 DG 資料接收路由，並使用你已完成的 DG Render Bridge 接收欄位。不能在沒有確認專案實際 HTTP 路由、資料庫及既有 Render 運作方式之前假設可以直接覆蓋 `server.mjs`。
