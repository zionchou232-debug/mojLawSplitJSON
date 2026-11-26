"""判決查詢的核心邏輯（不依賴 MCP，可單獨測試）。

資料來源：司法院「裁判書開放資料」（https://opendata.judicial.gov.tw/），
每月一包，每份裁判書是一個 JSON 檔，欄位有 JID、JYEAR、JCASE、JNO、JDATE、JTITLE、JFULL。
update_judgments.py 負責下載與匯入；這個檔案負責：

1. 解析 JID 與案號（「最高法院 112 年度台上字第 1234 號」「112台上1234」都能查）。
2. 判斷是不是刑事判決（只收刑事「判決」，預設不收裁定）。
3. 從全文抓出引用的法條（「刑法第339條」「毒品危害防制條例第四條」），
   法規名稱用 laws.db 的名稱與簡稱比對，所以能跟 get_article 串起來。
4. 搜尋：跟條文搜尋一樣切雙字詞交給 SQLite FTS5 做 BM25。
   為了省硬碟，只索引案由、主文和全文開頭（事實與理由的前段），全文另外壓縮保存。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from lawsearch import (
    _DIGITS, _UNITS, article_display, chinese_to_int, make_snippet, normalize_article_no,
    tokenize,
)

HERE = Path(__file__).resolve().parent
JUDGMENT_DB_PATH = Path(os.environ.get("JUDGMENT_DB_PATH", HERE / "judgments.db"))

FJUD_URL = "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id={jid}"

# 搜尋索引只收全文前面這麼多字（主文、事實、理由開頭），其餘只壓縮保存
INDEX_CHARS = 3000

SCHEMA = """
CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY,
    jid TEXT UNIQUE NOT NULL,
    court TEXT,
    court_code TEXT,
    level TEXT,            -- 最高法院 / 高等法院 / 地方法院 / 其他
    year INTEGER,          -- 民國年
    case_word TEXT,        -- 字別，例如 台上、上訴、易
    case_word_norm TEXT,   -- 臺→台 之後的字別，比對用
    case_no INTEGER,
    jdate TEXT,            -- YYYYMMDD
    title TEXT,            -- 案由
    kind TEXT,             -- 判決 / 裁定
    main_text TEXT,        -- 主文
    full_z BLOB,           -- 全文（zlib 壓縮）
    chars INTEGER,
    source TEXT            -- 從哪個壓縮檔匯入
);
CREATE INDEX IF NOT EXISTS idx_j_case ON judgments(year, case_word_norm, case_no);
CREATE INDEX IF NOT EXISTS idx_j_date ON judgments(jdate);
CREATE TABLE IF NOT EXISTS citations (
    judgment_id INTEGER NOT NULL,
    pcode TEXT NOT NULL,
    art_no TEXT NOT NULL,
    PRIMARY KEY (pcode, art_no, judgment_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ingested (
    archive TEXT PRIMARY KEY,
    month TEXT,
    added INTEGER,
    scanned INTEGER,
    at TEXT
);
-- 內容由 Python 先切成雙字詞、以空白分隔，FTS5 只負責 BM25（不另存原文，省空間）
CREATE VIRTUAL TABLE IF NOT EXISTS judgments_fts USING fts5(
    title, body, content='', tokenize='unicode61'
);
"""


# ---------------------------------------------------------------- 小工具

def norm_tai(s: str) -> str:
    return (s or "").replace("臺", "台")


def court_level(court: str, code: str = "") -> str:
    if "最高法院" in court or code[2:3] == "S":
        return "最高法院"
    if "高等法院" in court or code[2:3] == "H":
        return "高等法院"
    if "地方法院" in court or code[2:3] == "D":
        return "地方法院"
    return "其他"


def compress(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 9)


def decompress(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


def case_display(court: str, year: int, word: str, no: int, kind: str = "") -> str:
    tail = f"刑事{kind}" if kind else ""
    return f"{court} {year} 年度{word}字第 {no} 號{tail}"


# ---------------------------------------------------------------- 解析一份裁判書

_HEAD_RE = re.compile(r"\s+")
_COURT_RE = re.compile(r"^(.{2,20}?法院(?:[^\s刑民]{1,8}分院)?)")
_KIND_RE = re.compile(r"刑事(?:[^\s附]{0,6}?)?(判決|裁定)")
_MAIN_RE = re.compile(r"主\s*文\s*\n(.*?)(?:\n\s*(?:事\s*實|理\s*由|犯罪事實|事實及理由)\s*\n|$)", re.S)


@dataclass
class Judgment:
    jid: str
    court: str
    court_code: str
    level: str
    year: int
    case_word: str
    case_no: int
    jdate: str
    title: str
    kind: str
    main_text: str
    full: str


def classify(full: str) -> str | None:
    """從全文開頭判斷：刑事判決回傳「判決」，刑事裁定回傳「裁定」，其他（民事、行政、附帶民事）回傳 None。"""
    head = _HEAD_RE.sub("", full[:300])
    if "附帶民事" in head[:80]:
        return None
    m = _KIND_RE.search(head[:80])
    return m.group(1) if m else None


def parse_judgment(data: dict) -> Judgment | None:
    """把開放資料的一個 JSON 轉成 Judgment；不是刑事裁判或欄位不全時回傳 None。"""
    jid = (data.get("JID") or "").strip()
    full = (data.get("JFULL") or "").replace("\r\n", "\n").replace("\r", "\n")
    if not jid or not full:
        return None
    kind = classify(full)
    if kind is None:
        return None
    parts = jid.split(",")
    code = parts[0] if parts else ""
    try:
        year = int(data.get("JYEAR") or parts[1])
        no = int(data.get("JNO") or parts[3])
    except (ValueError, IndexError):
        return None
    word = (data.get("JCASE") or (parts[2] if len(parts) > 2 else "")).strip()
    jdate = re.sub(r"\D", "", str(data.get("JDATE") or (parts[4] if len(parts) > 4 else "")))[:8]
    first_line = full.lstrip().split("\n", 1)[0]
    m = _COURT_RE.match(_HEAD_RE.sub("", first_line))
    court = m.group(1) if m else code
    mm = _MAIN_RE.search(full)
    main_text = mm.group(1).strip() if mm else ""
    return Judgment(jid=jid, court=court, court_code=code, level=court_level(court, code),
                    year=year, case_word=word, case_no=no, jdate=jdate,
                    title=(data.get("JTITLE") or "").strip(), kind=kind,
                    main_text=main_text, full=full)


# ---------------------------------------------------------------- 語意向量用的段落

_FACT_HEAD_RE = re.compile(r"\n\s*(犯\s*罪\s*事\s*實|事\s*實\s*及\s*理\s*由|事\s*實|理\s*由)\s*(?:\n|[一壹１1][、.．])")
PASSAGE_CHARS = 1500


def fact_passage(full: str, title: str = "", max_chars: int = PASSAGE_CHARS) -> str:
    """送進語意模型的文字：案由 + 主文之後的事實／理由前段（去掉空白）。

    Colab 算向量和本機查詢都用這個函式，兩邊才會一致。
    """
    text = (full or "").replace("\r\n", "\n").replace("\r", "\n")
    m = _FACT_HEAD_RE.search(text)
    body = text[m.start():] if m else text
    body = re.sub(r"\s+", "", body)
    return (f"{title}：" if title else "") + body[:max_chars]


# ---------------------------------------------------------------- 法條引用

_NUM = "0-9０-９" + "".join(_DIGITS) + "".join(_UNITS)
_ART_RE = re.compile(rf"第\s*([{_NUM}]+)\s*條(?:\s*之\s*([{_NUM}]+))?")
_FW = str.maketrans("０１２３４５６７８９", "0123456789")


def _to_int(s: str) -> int | None:
    s = s.translate(_FW)
    return int(s) if s.isdigit() else chinese_to_int(s)


def extract_citations(text: str, name_index: dict[str, str], max_name_len: int = 30) -> set[tuple[str, str]]:
    """抓出全文中「<法規名稱>第N條（之M）」的引用，回傳 {(pcode, '185-3'), ...}。

    name_index 是「名稱或簡稱 → pcode」（LawDB.name_index）。往「第」前面找最長的已知法規名稱，
    所以「依刑法第339條」會認出「刑法」，「陸海空軍刑法第X條」不會被誤認成刑法。
    """
    out: set[tuple[str, str]] = set()
    for m in _ART_RE.finditer(text):
        before = re.sub(r"\s+", "", text[max(0, m.start() - max_name_len - 10):m.start()])
        before = before[-max_name_len:]
        pcode = None
        for i in range(len(before) - 1):  # 由長到短
            cand = before[i:]
            if cand in name_index:
                pcode = name_index[cand]
                break
        if pcode is None:
            continue
        main = _to_int(m.group(1))
        if main is None or main <= 0:
            continue
        no = str(main)
        if m.group(2):
            sub = _to_int(m.group(2))
            if sub is None:
                continue
            no = f"{main}-{sub}"
        out.add((pcode, no))
    return out


# ---------------------------------------------------------------- 寫入

def open_for_write(db_path: Path = JUDGMENT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")  # 更新時 MCP server 仍可同時讀
    conn.executescript(SCHEMA)
    return conn


def insert_judgment(conn: sqlite3.Connection, j: Judgment, name_index: dict[str, str],
                    source: str = "", index_chars: int = INDEX_CHARS) -> bool:
    """寫入一份判決；已經有同一個 JID 就跳過，回傳是否新增。"""
    if conn.execute("SELECT 1 FROM judgments WHERE jid=?", (j.jid,)).fetchone():
        return False
    cur = conn.execute(
        "INSERT INTO judgments (jid, court, court_code, level, year, case_word, case_word_norm,"
        " case_no, jdate, title, kind, main_text, full_z, chars, source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (j.jid, j.court, j.court_code, j.level, j.year, j.case_word, norm_tai(j.case_word),
         j.case_no, j.jdate, j.title, j.kind, j.main_text, compress(j.full), len(j.full), source),
    )
    rid = cur.lastrowid
    conn.execute(
        "INSERT INTO judgments_fts (rowid, title, body) VALUES (?,?,?)",
        (rid, " ".join(tokenize(j.title)), " ".join(tokenize(j.full[:index_chars]))),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO citations (judgment_id, pcode, art_no) VALUES (?,?,?)",
        [(rid, p, a) for p, a in extract_citations(j.full, name_index)],
    )
    return True


# ---------------------------------------------------------------- 案號解析

_CASE_RE = re.compile(
    r"^(?P<court>.*?(?:法院|分院))?\s*(?P<year>\d{2,3})\s*年?\s*度?\s*"
    r"(?P<word>[^\d\s]+?)\s*字?\s*第?\s*(?P<no>\d+)\s*號?"
)


def parse_case_number(text: str) -> dict | None:
    """「最高法院112年度台上字第1234號」「112台上1234」→ {'court','year','word','no'}。也接受 JID。"""
    s = (text or "").strip().translate(_FW)
    if s.count(",") >= 3:  # JID：TPSM,112,台上,1234,20230510,1
        p = s.split(",")
        try:
            return {"jid": s, "court_code": p[0], "year": int(p[1]), "word": p[2], "no": int(p[3])}
        except ValueError:
            return None
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(刑事)?(判決|裁定)$", "", s)
    m = _CASE_RE.match(s)
    if not m:
        return None
    word = m.group("word").removesuffix("年度").removesuffix("字")
    return {"court": m.group("court") or "", "year": int(m.group("year")),
            "word": word, "no": int(m.group("no"))}


# ---------------------------------------------------------------- 查詢

class JudgmentDB:
    def __init__(self, db_path: Path = JUDGMENT_DB_PATH, law_db=None, vectors=None):
        if not db_path.exists():
            raise FileNotFoundError(
                f"找不到判決資料庫 {db_path}，請先執行：python mcp_server/update_judgments.py"
            )
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.law_db = law_db
        self.vectors = vectors  # JudgmentVectors 或 None

    # ------------------------------------------------------------ 共用

    def _summary(self, r: sqlite3.Row) -> dict:
        return {
            "case": case_display(r["court"], r["year"], r["case_word"], r["case_no"], r["kind"]),
            "jid": r["jid"],
            "court": r["court"],
            "level": r["level"],
            "date": f"{r['jdate'][:4]}-{r['jdate'][4:6]}-{r['jdate'][6:8]}" if r["jdate"] else "",
            "title": r["title"],
            "main_text": make_snippet(r["main_text"] or "", [], width=150),
            "source_url": FJUD_URL.format(jid=quote(r["jid"], safe="")),
        }

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) n, MIN(jdate) a, MAX(jdate) b FROM judgments").fetchone()
        months = [r[0] for r in self.conn.execute("SELECT month FROM ingested ORDER BY month")]
        return {"judgments": row["n"], "earliest": row["a"], "latest": row["b"], "months": months}

    @staticmethod
    def _filters(court: str | None, level: str | None, year_from: int | None,
                 year_to: int | None) -> tuple[list[str], list]:
        where, params = [], []
        if court:
            where.append("REPLACE(j.court,'臺','台') LIKE ?")
            params.append(f"%{norm_tai(court)}%")
        if level:
            where.append("j.level = ?")
            params.append(level)
        if year_from:
            where.append("j.year >= ?")
            params.append(int(year_from))
        if year_to:
            where.append("j.year <= ?")
            params.append(int(year_to))
        return where, params

    # ------------------------------------------------------------ 依案號

    def get(self, case: str, court: str | None = None, offset: int = 0, max_chars: int = 20000) -> dict:
        q = parse_case_number(case)
        if q is None:
            return {"found": False, "reason": f"看不懂案號「{case}」，請用「最高法院112年度台上字第1234號」這種寫法"}
        if "jid" in q:
            rows = self.conn.execute("SELECT * FROM judgments WHERE jid=?", (q["jid"],)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM judgments WHERE year=? AND case_word_norm=? AND case_no=?"
                " ORDER BY jdate DESC",
                (q["year"], norm_tai(q["word"]), q["no"]),
            ).fetchall()
            want_court = norm_tai(court or q.get("court") or "")
            if want_court:
                narrowed = [r for r in rows if want_court in norm_tai(r["court"])
                            or norm_tai(r["court"]) in want_court]
                rows = narrowed or rows
        if not rows:
            return {"found": False,
                    "reason": "本機資料庫沒有這個案號（可能不是刑事判決、不在已下載的月份，或案號有誤）",
                    "coverage": self.stats()}
        if len(rows) > 1:
            return {"found": False, "reason": "有多個法院用同一個案號，請指定法院",
                    "candidates": [self._summary(r) for r in rows[:10]]}
        r = rows[0]
        full = decompress(r["full_z"])
        offset = max(0, int(offset))
        max_chars = max(1000, min(int(max_chars), 60000))
        part = full[offset:offset + max_chars]
        out = {"found": True, **self._summary(r), "main_text": r["main_text"],
               "full_text": part, "total_chars": len(full), "offset": offset}
        if offset + max_chars < len(full):
            out["next_offset"] = offset + max_chars
            out["note"] = "全文還沒完，用 next_offset 再取下一段"
        return out

    # ------------------------------------------------------------ 關鍵字搜尋

    def _rows_by_jids(self, jids: list[str], where: list[str], params: list) -> dict[str, sqlite3.Row]:
        out: dict[str, sqlite3.Row] = {}
        for i in range(0, len(jids), 500):
            chunk = jids[i:i + 500]
            cond = [f"j.jid IN ({','.join('?' * len(chunk))})"] + where
            for r in self.conn.execute(f"SELECT j.* FROM judgments j WHERE {' AND '.join(cond)}",
                                       chunk + params):
                out[r["jid"]] = r
        return out

    def _keyword_ranked(self, query: str, where: list[str], params: list
                        ) -> tuple[list[tuple[float, sqlite3.Row]], list[str], list[str]]:
        notes: list[str] = []
        expanded: list[str] = []
        if self.law_db is not None:
            _, expanded = self.law_db.expand_query(query)
            if expanded:
                notes.append("同義詞擴展：" + "、".join(expanded))
        base = list(dict.fromkeys(tokenize(query)))
        syn = [t for t in dict.fromkeys(tokenize(" ".join(expanded))) if t not in base]
        tokens = base + syn
        terms = [t for t in re.split(r"[\s,，、;；]+", query) if len(t) >= 2] + expanded
        if not tokens:
            return [], notes, terms
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        rows = self.conn.execute(
            f"""SELECT j.*, bm25(judgments_fts, 2.0, 1.0) AS bm
                FROM judgments_fts JOIN judgments j ON j.id = judgments_fts.rowid
                WHERE {' AND '.join(["judgments_fts MATCH ?"] + where)} ORDER BY bm LIMIT 300""",
            [match] + params,
        ).fetchall()
        scored = []
        for r in rows:
            head = decompress(r["full_z"])[:INDEX_CHARS]
            hay = set(tokenize(r["title"] + head))
            cov = sum(t in hay for t in base) / max(len(base), 1)
            hits = sum(1 for t in terms if t in r["title"] or t in head)
            score = -r["bm"] * (0.4 + cov) * (1 + 0.25 * min(hits, 4))
            score *= {"最高法院": 1.2, "高等法院": 1.1}.get(r["level"], 1.0)
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored, notes, terms

    def search(self, query: str, court: str | None = None, level: str | None = None,
               year_from: int | None = None, year_to: int | None = None, limit: int = 10,
               mode: str = "auto") -> dict:
        """mode：auto（有向量就混合）、hybrid、keyword（只用關鍵字）、semantic（只用語意）。"""
        limit = max(1, min(int(limit), 50))
        if mode not in ("auto", "hybrid", "keyword", "semantic"):
            return {"results": [], "notes": [f"不認得的搜尋模式「{mode}」"]}
        notes: list[str] = []
        if mode == "auto":
            mode = "hybrid" if self.vectors is not None else "keyword"
        elif mode != "keyword" and self.vectors is None:
            notes.append("還沒有判決向量（見 README「判決語意搜尋」），改用關鍵字搜尋")
            mode = "keyword"
        where, params = self._filters(court, level, year_from, year_to)

        kw: list[tuple[float, sqlite3.Row]] = []
        terms = [t for t in re.split(r"[\s,，、;；]+", query) if len(t) >= 2]
        if mode in ("hybrid", "keyword"):
            kw, kw_notes, terms = self._keyword_ranked(query, where, params)
            notes += kw_notes
        sem: list[tuple[sqlite3.Row, float]] = []
        if mode in ("hybrid", "semantic"):
            hits = self.vectors.search(query, top_k=1000)
            rows = self._rows_by_jids([j for j, _ in hits], where, params)
            sem = [(rows[j], sim) for j, sim in hits if j in rows]

        # 混合：Reciprocal Rank Fusion，兩邊名次都前面的排最前面
        fused: dict[str, dict] = {}
        for rank, (score, r) in enumerate(kw, 1):
            fused[r["jid"]] = {"row": r, "rrf": 1 / (60 + rank), "keyword_rank": rank}
        for rank, (r, sim) in enumerate(sem, 1):
            d = fused.setdefault(r["jid"], {"row": r, "rrf": 0.0})
            d["rrf"] += 1 / (60 + rank)
            d["semantic_rank"], d["similarity"] = rank, round(sim, 3)
        ranked = sorted(fused.values(), key=lambda d: -d["rrf"])

        results = []
        for d in ranked[:limit]:
            r = d["row"]
            out = self._summary(r)
            head = decompress(r["full_z"])
            out["snippet"] = make_snippet(head[:INDEX_CHARS] if terms else fact_passage(head)[:400],
                                          terms, width=120)
            out["match"] = {k: d[k] for k in ("keyword_rank", "semantic_rank", "similarity") if k in d}
            results.append(out)
        if not results:
            notes.append("沒有找到相關判決，可以換個說法、放寬法院或年份")
        return {"query": query, "mode": mode, "results": results, "notes": notes}

    def similar(self, case: str, court: str | None = None, level: str | None = None,
                year_from: int | None = None, year_to: int | None = None, limit: int = 10) -> dict:
        """找跟某件判決事實最像的其他判決（要有向量）。"""
        if self.vectors is None:
            return {"results": [], "notes": ["還沒有判決向量，無法找相似判決（見 README「判決語意搜尋」）"]}
        base = self.get(case, max_chars=1000)
        if not base.get("found"):
            return {"results": [], "notes": [base.get("reason", "找不到這個案號")],
                    "candidates": base.get("candidates", [])}
        hits = self.vectors.similar_to(base["jid"], top_k=1000)
        if hits is None:
            return {"results": [], "notes": ["這件判決還沒有向量（可能是 Colab 還沒算到那個月）"]}
        where, params = self._filters(court, level, year_from, year_to)
        rows = self._rows_by_jids([j for j, _ in hits], where, params)
        results = []
        for jid, sim in hits:
            if jid in rows:
                out = self._summary(rows[jid])
                out["snippet"] = fact_passage(decompress(rows[jid]["full_z"]))[:300]
                out["similarity"] = round(sim, 3)
                results.append(out)
                if len(results) >= max(1, min(int(limit), 50)):
                    break
        return {"base": base["case"], "results": results, "notes": []}

    # ------------------------------------------------------------ 依法條

    def by_article(self, law: str, article: str, court: str | None = None, level: str | None = None,
                   year_from: int | None = None, year_to: int | None = None, limit: int = 10) -> dict:
        if self.law_db is None:
            return {"results": [], "notes": ["沒有載入法規資料庫（laws.db），無法解析法規名稱"]}
        lw, cands = self.law_db.resolve_law(law)
        if lw is None:
            return {"results": [], "notes": [f"找不到法規「{law}」"],
                    "law_candidates": [c.name for c in cands]}
        no = normalize_article_no(article)
        if no is None:
            return {"results": [], "notes": [f"看不懂條號「{article}」"]}
        limit = max(1, min(int(limit), 50))
        where, params = self._filters(court, level, year_from, year_to)
        where = ["c.pcode = ?", "c.art_no = ?"] + where
        params = [lw.pcode, no] + params
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM citations c JOIN judgments j ON j.id=c.judgment_id WHERE {' AND '.join(where)}",
            params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT j.* FROM citations c JOIN judgments j ON j.id = c.judgment_id
                WHERE {' AND '.join(where)}
                ORDER BY CASE j.level WHEN '最高法院' THEN 0 WHEN '高等法院' THEN 1 ELSE 2 END,
                         j.jdate DESC
                LIMIT ?""",
            params + [limit],
        ).fetchall()
        label = article_display(no)
        results = []
        for r in rows:
            d = self._summary(r)
            full = decompress(r["full_z"])
            main = label.split("之")[0]
            pos = full.find(main)
            d["snippet"] = make_snippet(full[pos:] if pos > 0 else full, [main], width=120)
            results.append(d)
        notes = [f"共有 {total} 份判決引用{lw.name}{label}，依法院層級、日期新到舊排序"]
        return {"law": lw.name, "article": label, "total": total, "results": results, "notes": notes}
