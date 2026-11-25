"""台灣法規 MCP server：精準查條文 + 白話搜尋條文。

啟動（stdio）：python mcp_server/server.py
第一次使用前請先建索引：python mcp_server/build_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from lawsearch import LawDB  # noqa: E402

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

server = MCPServer(
    name="taiwan-law-search",
    instructions=(
        "查詢中華民國（台灣）現行法規條文。引用法條前，先用 get_article 取得原文，"
        "不要憑記憶引用。不知道條號時用 search_articles 以白話搜尋。"
        "回傳的 source_url 是全國法規資料庫的官方連結，回答時請附上。"
    ),
)

_db: LawDB | None = None


def db() -> LawDB:
    global _db
    if _db is None:
        _db = LawDB()
    return _db


@server.tool(annotations=READ_ONLY)
def get_article(law: str, article: str) -> dict:
    """取得某部法規某一條的官方條文原文。

    Args:
        law: 法規名稱，可用全名或簡稱，例如「中華民國刑法」「刑法」「刑訴」「跟騷法」，或法規代碼 C0000001。
        article: 條號，接受「271」「第271條」「185-3」「185之3」「第一百八十五條之三」等寫法。

    找不到時 found 為 false，並附上可能的法規名稱或附近條號；請不要自行補上條文內容。
    """
    return db().get_article(law, article)


@server.tool(annotations=READ_ONLY)
def search_articles(
    query: str,
    law: str | None = None,
    nature: Literal["憲法", "法律", "命令"] | None = None,
    include_abolished: bool = False,
    limit: int = 10,
    mode: Literal["auto", "hybrid", "keyword", "semantic"] = "auto",
) -> dict:
    """用白話或關鍵字搜尋相關法條，例如「酒駕撞死人」「打架把人打成重傷」「罰3萬」。

    結合兩種搜尋：關鍵字（BM25）找字面命中的條文，語意搜尋找意思相近的條文，
    再依法規位階、常用法規加權排序。口語詞會換成法條用語（酒駕 → 不能安全駕駛），
    阿拉伯數字會換成條文用的國字（3年 → 三年）。查詢中提到法規名稱時，該法規優先。

    Args:
        query: 查詢內容，可以是白話描述或關鍵字，多個詞用空白分隔。
        law: 只搜尋這部法規（全名或簡稱）。
        nature: 只搜尋某種位階：憲法、法律（立法院通過）、命令（行政規則、辦法等）。
        include_abolished: 是否包含已廢止的法規，預設不含。
        limit: 回傳筆數，1 到 50，預設 10。
        mode: auto（預設，有語意向量就混合搜尋）、hybrid、keyword（只用關鍵字）、semantic（只用語意）。
    """
    return db().search(query, law=law, nature=nature,
                       include_abolished=include_abolished, limit=limit, mode=mode)


@server.tool(annotations=READ_ONLY)
def find_law(name: str, include_abolished: bool = False) -> dict:
    """依名稱或簡稱找法規，回傳全名、代碼、位階、最新異動日期與官方網址。名稱不確定時先用這個。"""
    laws = db().find_laws(name, include_abolished=include_abolished)
    return {
        "results": [
            {"name": x.name, "pcode": x.pcode, "nature": x.nature, "category": x.category,
             "updated": x.updated, "abolished": x.abolished, "url": x.url}
            for x in laws
        ]
    }


if __name__ == "__main__":
    server.run("stdio")
