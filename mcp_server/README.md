# 台灣法規搜尋 MCP server

用這個 repo 的全國法規資料（`FalVMingLing/*.json`，11,805 部法規、22 萬條條文）做的 MCP server，
讓 Claude 等 AI 助理能**查到正確的條文原文**，也能**用白話找到相關法條**。資料都在本機，不連網。

## 工具

| 工具 | 用途 | 例子 |
|---|---|---|
| `get_article` | 取某法某條的原文，附官方連結。找不到就明說，不讓 AI 自己編 | `刑法` + `第一百八十五條之三` |
| `search_articles` | 白話或關鍵字搜尋條文 | `酒駕撞死人`、`偷拍`、`羈押 期間`（限 `刑訴`） |
| `find_law` | 依名稱或簡稱找法規 | `毒品`、`跟騷法` |
| `search_judgments` | 關鍵字或白話搜尋刑事判決，可篩法院、審級、年度 | `酒駕 撞死`、`詐騙 車手` |
| `get_judgment` | 依案號取判決全文（太長會分段） | `最高法院112年度台上字第1234號`、`112台上1234` |
| `find_similar_judgments` | 找跟某件判決事實相近的判決（需要判決向量） | `最高法院112年度台上字第1234號` |
| `find_judgments_by_article` | 找引用某條文的判決，最高法院優先、新到舊 | `刑法` + `339之4` |

### 檢察官實務工具（罪名卡）

| 工具 | 用途 | 例子 |
|---|---|---|
| `list_crimes` | 列出已有的罪名卡與審閱狀態 | |
| `get_crime_elements` | 某罪的構成要件拆解：各要件說明、應蒐集證據、常見爭點、起訴前確認、罪數競合、沒收，附條文原文 | `酒駕`、`339-4`、`人頭帳戶` |
| `get_evidence_checklist` | 蒐證檢核清單（markdown 勾選格式，可存檔列印） | `施用毒品` |

罪名卡在 `mcp_server/crimes/*.json`，一罪一檔。目前有：不能安全駕駛（酒駕、毒駕）、加重詐欺（車手、收水）、
提供帳戶幫助詐欺洗錢、施用毒品、販賣毒品、竊盜。卡片由 Claude 起草，`status` 是 `draft`；
審閱修改後把 `status` 改成 `reviewed`、`reviewed_by` 填上名字。條文原文不寫進卡片，查詢時從 `laws.db` 即時取出，
所以條文修正後只要重建索引。`precedents` 先留空，之後從判決資料庫統計常引見解再補，每筆要標 `verified`。

**偵查不公開**：這些工具只接受罪名或條號，不接受案情。勾選清單請在本機做，不要把卷證內容貼進對話。

判決工具要先建好判決資料庫（見下方「判決資料庫」）；沒建的話，這三個工具會提示怎麼建，條文工具照常可用。

## 搜尋怎麼強化的（參考 TaiLexi AI 的公開設計）

1. **法規名稱解析**：全名、簡稱（`aliases.json` 加內建的刑法、刑訴、民訴…）、去掉「中華民國」、模糊比對。
2. **條號正規化**：`271`、`第271條`、`185-3`、`185之3`、`第一百八十五條之三`、全形數字都通。
3. **中文切詞**：不用斷詞套件，切成雙字詞後交給 SQLite FTS5 用 BM25 排序。
4. **同義詞擴展**：`synonyms.json` 把口語換成法條用語（酒駕 → 不能安全駕駛、偷 → 竊取），長詞優先比對。
5. **多層加權重排**：BM25 × 查詢詞命中率 × 整詞命中 × 法規位階（憲法 > 法律 > 命令）× 常用法規（刑法、刑訴…）× 查詢中點名的法規。
6. 預設排除已廢止法規與「（刪除）」條文。
7. **數字對照**：查詢的阿拉伯數字換成條文用的國字（`3年` → `三年`、`30萬` → `三十萬`、`110` → `一百十`）。
8. **語意搜尋（選用）**：用 `BAAI/bge-small-zh-v1.5` 把條文轉成向量，白話問題（「被打的時候還手」）也找得到意思相近的條文；
   再用 Reciprocal Rank Fusion 和關鍵字結果合併排名。沒建向量時自動只用關鍵字。

## 開啟語意搜尋

```bash
pip install -r mcp_server/requirements-semantic.txt
python mcp_server/build_embeddings.py --limit 500   # 先試跑，第一次會下載模型（約 100MB）
python mcp_server/build_embeddings.py               # 憲法＋法律層級約 4 萬條，CPU 約 10–20 分鐘
python mcp_server/eval_search.py                    # 比較 keyword / semantic / hybrid 的命中率
```

向量存在 `mcp_server/vectors/`（不進 git）。加 `--all` 連命令層級一起建（約 16 萬條，較久）。
`search_articles` 的 `mode` 參數可指定 `hybrid`、`keyword` 或 `semantic`。

## 安裝與使用（Windows 也一樣）

```bash
git clone https://github.com/zionchou232-debug/mojLawSplitJSON.git
cd mojLawSplitJSON
pip install -r mcp_server/requirements.txt
python mcp_server/build_index.py      # 建索引，約 20 秒，產生 mcp_server/laws.db（約 200MB）
python -m pytest mcp_server/tests     # 測試
```

### 接到 Claude Code

```bash
claude mcp add taiwan-law -- python /完整路徑/mojLawSplitJSON/mcp_server/server.py
```

### 接到 Claude Desktop

編輯 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "taiwan-law": {
      "command": "python",
      "args": ["C:\\完整路徑\\mojLawSplitJSON\\mcp_server\\server.py"]
    }
  }
}
```

## 更新資料

上游 [kong0107/mojLawSplitJSON](https://github.com/kong0107/mojLawSplitJSON) 會定期更新。在 GitHub 按 Sync fork 或 `git pull` 後，重跑 `build_index.py` 即可。

## 判決資料庫

資料來源是[司法院裁判書開放資料](https://opendata.judicial.gov.tw/)，每月發布一包壓縮檔。
`update_judgments.py` 會下載、只挑**刑事判決**（不收裁定、民事、行政、附帶民事），存進 `mcp_server/judgments.db`。

**空間怎麼省**

- 原始壓縮檔放 `--raw-dir`，建議指向 **Google Drive 電腦版的串流資料夾**（例如 `G:\我的雲端硬碟\判決原始檔`），檔案只佔雲端空間。
- 本機只留 `judgments.db`：判決全文用 zlib 壓縮；搜尋索引只收案由、主文與全文前 3000 字（`--index-chars` 可調）。
- 解壓時會在本機暫存一個月份的檔案，處理完立刻刪掉。暫存位置可用 `--temp-dir` 指定。

**第一次建立**（先跑一個月試試看大小）

```bash
# 先確認平台上有哪些月份（不下載）
python mcp_server/update_judgments.py --probe
# 下載並匯入一個月，看實際大小
python mcp_server/update_judgments.py --raw-dir "G:\我的雲端硬碟\判決原始檔" --max-new 1
python mcp_server/update_judgments.py --stats
# 沒問題就跑完近 3 年（預設 --months 36）
python mcp_server/update_judgments.py --raw-dir "G:\我的雲端硬碟\判決原始檔"
```

- 平台若要求會員登入，先在平台註冊，再設定環境變數 `JUDICIAL_USER`、`JUDICIAL_PASSWORD`。
- 自動下載失敗時，可以用瀏覽器從平台手動下載壓縮檔丟進 `--raw-dir`，再加 `--no-download` 執行。
- 解壓 rar 用 Windows 10 以上內建的 `tar` 指令，不用另外裝軟體。
- 重跑不會重複下載或重複匯入；已匯入的壓縮檔記在資料庫的 `ingested` 表。
- 法條引用要靠 `laws.db` 認法規名稱，所以要先跑過 `build_index.py`。

**每兩週自動更新（Windows 工作排程器）**

在「命令提示字元」執行一次（路徑換成你自己的；`python.exe` 用虛擬環境裡的那個）：

```bat
schtasks /Create /TN "判決資料庫更新" /SC DAILY /MO 14 /ST 03:00 /TR "\"C:\完整路徑\mojLawSplitJSON\.venv\Scripts\python.exe\" \"C:\完整路徑\mojLawSplitJSON\mcp_server\update_judgments.py\" --raw-dir \"G:\我的雲端硬碟\判決原始檔\""
```

開放資料一個月發布一次，每 14 天檢查一次就不會漏。電腦在排定時間關機的話，到「工作排程器」裡把這個工作的「錯過排程時儘快執行」打勾。
更新時 MCP server 可以照常使用（資料庫用 WAL 模式）。

## 判決語意搜尋（用 Colab 的 GPU 算向量）

關鍵字很難找「事實相近」的案子，所以判決也可以做語意搜尋。判決量大，電腦的 CPU 算太慢，改用 Google Colab 免費 GPU：

1. 在 Google Drive 打開 [`mcp_server/colab/judgment_embeddings.ipynb`](colab/judgment_embeddings.ipynb)
   （或在 Colab「檔案 → 開啟筆記本 → GitHub」貼上本 repo 網址），執行階段選 **T4 GPU**，按「全部執行」。
2. 它會讀 Google Drive 的「判決原始檔」，挑出刑事判決，用 **BAAI/bge-m3** 算向量，每個月存一組到「判決向量」資料夾。
   **斷線重跑會自動接續**，已算好的月份會跳過。
3. 電腦上：`pip install -r mcp_server/requirements-semantic.txt`，並把 MCP server 的環境變數
   `JUDGMENT_VECTOR_DIR` 設成 `G:\我的雲端硬碟\判決向量`（Claude Desktop 設定裡的 `"env"`）。
   第一次查詢會下載 bge-m3 的 ONNX 檔（約 2.3GB），之後查詢只用 CPU。

有向量時，`search_judgments` 預設混合關鍵字與語意（Reciprocal Rank Fusion），也可以用 `mode="semantic"` 直接描述案情；
`find_similar_judgments` 找跟某件判決事實相近的案子。送進模型的文字是「案由＋事實／理由前 1500 字」（`judgments.fact_passage`），
Colab 跟本機用同一個函式。向量用 float16 存、查詢時分塊計算，不會一次吃掉大量記憶體。

## 還能再做的

- 條文中「第X條」交叉引用的連結
