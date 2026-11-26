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

from crimes import CrimeBook  # noqa: E402
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
        "檢察官實務：get_crime_elements 取某罪的構成要件拆解，get_evidence_checklist 取蒐證檢核清單，"
        "list_crimes 列出已有的罪名卡。偵查不公開：不要要求使用者提供或貼上案情、卷證內容。"
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
        try:
            from judgment_vectors import try_load as load_judgment_vectors
            vectors = load_judgment_vectors()
        except Exception:  # noqa: BLE001  沒裝 numpy 等情況，退回只用關鍵字
            vectors = None
        _jdb = JudgmentDB(JUDGMENT_DB_PATH, law_db=law_db, vectors=vectors)
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
    mode: Literal["auto", "hybrid", "keyword", "semantic"] = "auto",
) -> dict:
    """用關鍵字或白話搜尋刑事判決，例如「酒駕 累犯 易科罰金」，或直接描述案情
    「朋友借車給我，我喝酒後開車撞到機車騎士」找事實相近的判決。

    搜尋範圍是案由、主文與判決前段（事實、理由開頭）。有判決向量時預設混合關鍵字與語意搜尋。

    Args:
        query: 查詢內容，多個詞用空白分隔。
        court: 只找某法院，可寫部分名稱，例如「臺北地方法院」「高等法院臺中分院」。
        level: 只找某審級：最高法院、高等法院、地方法院。
        year_from: 從民國幾年度起（案號年度），例如 112。
        year_to: 到民國幾年度止。
        limit: 回傳筆數，1 到 50，預設 10。
        mode: auto（預設）、hybrid、keyword（只用關鍵字）、semantic（只用語意，適合描述案情）。
    """
    j = jdb()
    if j is None:
        return NO_JDB
    return j.search(query, court=court, level=level, year_from=year_from, year_to=year_to,
                    limit=limit, mode=mode)


@server.tool(annotations=READ_ONLY)
def find_similar_judgments(
    case: str,
    court: str | None = None,
    level: Level | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 10,
) -> dict:
    """找跟某件判決事實最相近的其他刑事判決（語意比對，需要判決向量）。

    Args:
        case: 案號，例如「最高法院112年度台上字第1234號」。
        court, level, year_from, year_to, limit: 同 search_judgments。
    """
    j = jdb()
    if j is None:
        return NO_JDB
    return j.similar(case, court=court, level=level, year_from=year_from, year_to=year_to, limit=limit)


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


_book: CrimeBook | None = None


def book() -> CrimeBook:
    global _book
    if _book is None:
        _book = CrimeBook.load()
    return _book


def _law_db_or_none() -> LawDB | None:
    try:
        return db()
    except FileNotFoundError:
        return None


@server.tool(annotations=READ_ONLY)
def list_crimes() -> dict:
    """列出目前有哪些罪名卡（構成要件拆解與蒐證檢核清單），以及每張卡是否已經檢察官審閱。"""
    return {"results": book().list()}


@server.tool(annotations=READ_ONLY)
def get_crime_elements(crime: str, include_article_text: bool = True) -> dict:
    """取得某罪的構成要件拆解：客觀、主觀要件、加重要件，各要件的說明、應蒐集證據與常見爭點，
    以及起訴前確認事項、罪數競合、沒收注意事項。相關條文的原文從法規資料庫即時附上。

    status 為 draft 時，內容是尚未經檢察官審閱的草稿，回答時要提醒使用者。
    只接受罪名或條號，不需要也不要要求案情。

    Args:
        crime: 罪名、簡稱或條號，例如「酒駕」「加重詐欺」「人頭帳戶」「施用毒品」「339-4」「320」。
        include_article_text: 是否附上相關條文原文，預設是。
    """
    return book().get(crime, law_db=_law_db_or_none() if include_article_text else None,
                      include_article_text=include_article_text)


@server.tool(annotations=READ_ONLY)
def get_evidence_checklist(crime: str, format: Literal["markdown", "json"] = "markdown") -> dict:
    """取得某罪的蒐證檢核清單，依構成要件分組，另附起訴前確認事項與沒收事項。

    markdown 格式用「- [ ]」排版，可以直接存檔或印出來勾選。勾選請在本機進行，不要把案情貼回對話。

    Args:
        crime: 罪名、簡稱或條號，同 get_crime_elements。
        format: markdown（預設，附 markdown 欄位）或 json（只回分組資料）。
    """
    return book().checklist(crime, fmt=format)


if __name__ == "__main__":
    server.run("stdio")
