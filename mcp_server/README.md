# 台灣法規搜尋 MCP server

用這個 repo 的全國法規資料（`FalVMingLing/*.json`，11,805 部法規、22 萬條條文）做的 MCP server，
讓 Claude 等 AI 助理能**查到正確的條文原文**，也能**用白話找到相關法條**。資料都在本機，不連網。

## 工具

| 工具 | 用途 | 例子 |
|---|---|---|
| `get_article` | 取某法某條的原文，附官方連結。找不到就明說，不讓 AI 自己編 | `刑法` + `第一百八十五條之三` |
| `search_articles` | 白話或關鍵字搜尋條文 | `酒駕撞死人`、`偷拍`、`羈押 期間`（限 `刑訴`） |
| `find_law` | 依名稱或簡稱找法規 | `毒品`、`跟騷法` |

## 搜尋怎麼強化的（參考 TaiLexi AI 的公開設計）

1. **法規名稱解析**：全名、簡稱（`aliases.json` 加內建的刑法、刑訴、民訴…）、去掉「中華民國」、模糊比對。
2. **條號正規化**：`271`、`第271條`、`185-3`、`185之3`、`第一百八十五條之三`、全形數字都通。
3. **中文切詞**：不用斷詞套件，切成雙字詞後交給 SQLite FTS5 用 BM25 排序。
4. **同義詞擴展**：`synonyms.json` 把口語換成法條用語（酒駕 → 不能安全駕駛、偷 → 竊取），長詞優先比對。
5. **多層加權重排**：BM25 × 查詢詞命中率 × 整詞命中 × 法規位階（憲法 > 法律 > 命令）× 常用法規（刑法、刑訴…）× 查詢中點名的法規。
6. 預設排除已廢止法規與「（刪除）」條文。

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

## 還能再做的

- 語意向量搜尋（embedding），處理同義詞表沒收錄的說法
- 判決資料（司法院裁判書開放資料）
- 條文中「第X條」交叉引用的連結
