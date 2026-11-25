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

from judgments import JUDGMENT_DB_PATH, JudgmentDB  # noqa: E402
from lawsearch import LawDB  # noqa: E402

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

server = MCPServer(
    name="taiwan-law-search",
    instructions=(
        "查詢中華民國（台灣）現行法規條文。引用法條前，先用 get_article 取得原文，"
        "不要憑記憶引用。不知道條號時用 search_articles 以白話搜尋。"
        "回傳的 source_url 是全國法規資料庫的官方連結，回答時請附上。"
        "要找實務見解時，用 search_judgments 搜判決、find_judgments_by_article 找引用某條文的判決，"
        "get_judgment 依案號取全文。判決只收本機已下載的刑事判決，引用時附上 source_url（司法院裁判書查詢）。"
    ),
)

_db: LawDB | None = None
_jdb: JudgmentDB | None = None


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


def jdb() -> JudgmentDB | None:
    global _jdb
    if _jdb is None and JUDGMENT_DB_PATH.exists():
        try:
            law_db = db()
        except FileNotFoundError:
            law_db = None
        _jdb = JudgmentDB(JUDGMENT_DB_PATH, law_db=law_db)
    return _jdb


NO_JDB = {"results": [], "notes": [
    "還沒有判決資料庫。請先執行 python mcp_server/update_judgments.py --raw-dir <原始檔資料夾>"]}

Level = Literal["最高法院", "高等法院", "地方法院"]


@server.tool(annotations=READ_ONLY)
def search_judgments(
    query: str,
    court: str | None = None,
    level: Level | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 10,
) -> dict:
    """用關鍵字或白話搜尋刑事判決，例如「酒駕 累犯 易科罰金」「詐騙 車手 加重詐欺」。

    搜尋範圍是案由、主文與判決前段（事實、理由開頭）。最高法院、高等法院的判決會稍微優先。

    Args:
        query: 查詢內容，多個詞用空白分隔。
        court: 只找某法院，可寫部分名稱，例如「臺北地方法院」「高等法院臺中分院」。
        level: 只找某審級：最高法院、高等法院、地方法院。
        year_from: 從民國幾年度起（案號年度），例如 112。
        year_to: 到民國幾年度止。
        limit: 回傳筆數，1 到 50，預設 10。
    """
    j = jdb()
    if j is None:
        return NO_JDB
    return j.search(query, court=court, level=level, year_from=year_from, year_to=year_to, limit=limit)


@server.tool(annotations=READ_ONLY)
def get_judgment(case: str, court: str | None = None, offset: int = 0, max_chars: int = 20000) -> dict:
    """依案號取刑事判決全文，例如「最高法院112年度台上字第1234號」「112台上1234」。

    判決很長時會分段回傳，用回傳的 next_offset 再呼叫一次取下一段。
    找不到時 found 為 false；請不要自行編造判決內容。

    Args:
        case: 案號，「臺」「台」都可以，也接受司法院的 JID（TPSM,112,台上,1234,20230510,1）。
        court: 同一案號有多個法院時，指定法院名稱。
        offset: 從全文第幾個字開始。
        max_chars: 這次最多回傳幾個字，預設 20000。
    """
    j = jdb()
    if j is None:
        return {"found": False, "reason": NO_JDB["notes"][0]}
    return j.get(case, court=court, offset=offset, max_chars=max_chars)


@server.tool(annotations=READ_ONLY)
def find_judgments_by_article(
    law: str,
    article: str,
    court: str | None = None,
    level: Level | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 10,
) -> dict:
    """找引用某法某條的刑事判決，例如 刑法 第339條之4、毒品危害防制條例 第4條。

    依法院層級（最高法院優先）、日期新到舊排序，並回傳總共有幾份判決引用。

    Args:
        law: 法規名稱或簡稱，跟 get_article 一樣，例如「刑法」「毒品條例」。
        article: 條號，例如「339-4」「第三百三十九條之四」。
        court, level, year_from, year_to, limit: 同 search_judgments。
    """
    j = jdb()
    if j is None:
        return NO_JDB
    return j.by_article(law, article, court=court, level=level,
                        year_from=year_from, year_to=year_to, limit=limit)


if __name__ == "__main__":
    server.run("stdio")
