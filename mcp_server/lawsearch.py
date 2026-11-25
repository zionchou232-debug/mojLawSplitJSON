"""法規查詢與搜尋的核心邏輯（不依賴 MCP，可單獨測試）。

資料來源：本 repo 的 FalVMingLing/*.json（全國法規資料庫）與 aliases.json。
索引：build_index.py 產生的 SQLite 檔（laws.db），內含 FTS5 全文索引。

搜尋設計（參考 TaiLexi AI 公開描述的「白話搜尋」與「多層加權」）：
1. 法規名稱解析：全名、簡稱（aliases.json + 內建補充）、去掉「中華民國」前綴、模糊比對。
2. 條號正規化：271、第271條、271-1、271之1、第二百七十一條之一 都能查到同一條。
3. 中文切詞：不用斷詞套件，直接切「雙字詞」（bigram），FTS5 以 BM25 排序。
4. 同義詞擴展：口語詞（酒駕、偷東西）對應到法條用語（不能安全駕駛、竊取）。
5. 多層加權重排：法規位階、常用法規、命中率（覆蓋度）、整詞命中、法規名稱命中。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LAW_DATA_DIR", HERE.parent))
DB_PATH = Path(os.environ.get("LAW_DB_PATH", HERE / "laws.db"))
SYNONYMS_PATH = HERE / "synonyms.json"

MOJ_SINGLE = "https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode={pcode}&flno={flno}"

# aliases.json 沒收錄、但很常用的簡稱
EXTRA_ALIASES = {
    "C0000001": ["刑法"],
    "C0010001": ["刑事訴訟法", "刑訴", "刑訴法"],
    "B0000001": ["民法"],
    "B0010001": ["民事訴訟法", "民訴", "民訴法"],
    "A0000001": ["憲法"],
}

# 常用法規加權（刑事為主，順帶民事與程序法）
POPULAR_LAWS = {
    "C0000001": 1.35,  # 中華民國刑法
    "C0010001": 1.3,   # 刑事訴訟法
    "C0000008": 1.2,   # 毒品危害防制條例
    "D0080047": 1.15,  # 槍砲彈藥刀械管制條例
    "K0040012": 1.15,  # 道路交通管理處罰條例
    "B0000001": 1.25,  # 民法
    "B0010001": 1.2,   # 民事訴訟法
    "A0000001": 1.3,   # 中華民國憲法
    "A0030055": 1.15,  # 行政程序法
}

NATURE_WEIGHT = {"憲法": 1.3, "法律": 1.15, "命令": 1.0}

CJK = r"㐀-䶿一-鿿豈-﫿"
_TOKEN_RE = re.compile(rf"[{CJK}]+|[A-Za-z0-9]+")


# ---------------------------------------------------------------- 切詞

def tokenize(text: str) -> list[str]:
    """把中文切成雙字詞，英數字保留整個單字。單一中文字也保留，避免一字查不到。"""
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text or ""):
        seg = m.group(0)
        if re.match(rf"[{CJK}]", seg):
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            out.append(seg.lower())
    return out


# ---------------------------------------------------------------- 中文數字與條號

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}


def chinese_to_int(s: str) -> int | None:
    """二百七十一 → 271；二七一 → 271；十 → 10。無法解析回傳 None。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if all(ch in _DIGITS for ch in s):  # 逐位寫法：二七一
        return int("".join(str(_DIGITS[ch]) for ch in s))
    total, num = 0, 0
    for ch in s:
        if ch in _DIGITS:
            num = _DIGITS[ch]
        elif ch in _UNITS:
            total += (num or 1) * _UNITS[ch]
            num = 0
        else:
            return None
    return total + num


def normalize_article_no(raw: str) -> str | None:
    """把各種條號寫法轉成資料裡的格式：'185-3'。"""
    s = str(raw).strip()
    s = s.translate(str.maketrans("０１２３４５６７８９－", "0123456789-"))
    s = re.sub(r"\s+", "", s)
    s = s.removeprefix("第")
    s = re.sub(r"條", "", s, count=1)
    m = re.fullmatch(rf"([0-9{''.join(_DIGITS)}{''.join(_UNITS)}]+)(?:(?:之|-|的)([0-9{''.join(_DIGITS)}{''.join(_UNITS)}]+))?", s)
    if not m:
        return None
    main = chinese_to_int(m.group(1))
    if main is None:
        return None
    if m.group(2):
        sub = chinese_to_int(m.group(2))
        if sub is None:
            return None
        return f"{main}-{sub}"
    return str(main)


def article_label_to_no(label: str) -> str:
    """資料裡的『第 185-3 條』→ '185-3'。"""
    return re.sub(r"[第條\s]", "", label)


def article_display(no: str) -> str:
    main, _, sub = no.partition("-")
    return f"第{main}條" + (f"之{sub}" if sub else "")


_CN_DIGITS = "零一二三四五六七八九"


def _four_digits(n: int) -> str:
    """0 < n < 10000 → 國字，例如 305 → 三百零五。"""
    out, zero = "", False
    for unit_val, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        d = n // unit_val % 10
        if d:
            if zero and out:
                out += "零"
            if unit == "十" and d == 1 and out and not out.endswith("零"):
                out += "十"  # 法條慣用「一百十條」而非「一百一十條」
            else:
                out += _CN_DIGITS[d] + unit
            zero = False
        elif out:
            zero = True
    return out


def int_to_chinese(n: int) -> str:
    """法條常見寫法：3 → 三、10 → 十、15 → 十五、300000 → 三十萬、2000000 → 二百萬。"""
    if n == 0:
        return "零"
    parts, groups = [], [(n // 10**8) % 10**4, (n // 10**4) % 10**4, n % 10**4]
    for g, unit in zip(groups, ("億", "萬", "")):
        if g:
            if parts and g < 1000:
                parts.append("零")
            parts.append(_four_digits(g) + unit)
    s = "".join(parts)
    return s[1:] if s.startswith("一十") else s


def numbers_to_chinese(text: str) -> str:
    """把查詢裡的阿拉伯數字換成國字：『3年』→『三年』、『0.25』→『零點二五』。"""
    text = text.translate(str.maketrans("０１２３４５６７８９．", "0123456789."))

    def repl(m: re.Match) -> str:
        whole, frac = m.group(1).replace(",", ""), m.group(2)
        s = int_to_chinese(int(whole))
        if frac:
            s += "點" + "".join(_CN_DIGITS[int(c)] for c in frac)
        return s

    return re.sub(r"(\d[\d,]*)(?:\.(\d+))?", repl, text)


# ---------------------------------------------------------------- 資料庫

def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"找不到索引檔 {db_path}，請先執行：python mcp_server/build_index.py"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def load_synonyms(path: Path = SYNONYMS_PATH) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


@dataclass
class Law:
    pcode: str
    name: str
    nature: str
    category: str
    url: str
    updated: str
    abolished: bool


class LawDB:
    def __init__(self, db_path: Path = DB_PATH, synonyms_path: Path = SYNONYMS_PATH,
                 vectors=None, load_vectors: bool = True):
        self.conn = connect(db_path)
        self.synonyms = load_synonyms(synonyms_path)
        self.laws: dict[str, Law] = {}
        self.name_index: dict[str, str] = {}  # 名稱或簡稱 → pcode
        for r in self.conn.execute("SELECT * FROM laws"):
            law = Law(r["pcode"], r["name"], r["nature"], r["category"], r["url"],
                      r["updated"], bool(r["abolished"]))
            self.laws[law.pcode] = law
            for alias in json.loads(r["aliases"]):
                # 同名時，現行法規優先於已廢止的
                prev = self.name_index.get(alias)
                if prev is None or (self.laws[prev].abolished and not law.abolished):
                    self.name_index[alias] = law.pcode
        # 長的名稱先比對，避免「刑法」搶走「陸海空軍刑法」
        self._names_by_len = sorted(self.name_index, key=len, reverse=True)
        # 語意向量：傳入 vectors 就用，否則試著載入 mcp_server/vectors/ 下的檔案
        if vectors is None and load_vectors:
            try:
                from semantic import try_load
                vectors = try_load()
            except Exception:  # 沒裝 numpy/fastembed 或檔案壞了，就只用關鍵字
                vectors = None
        self.vectors = vectors

    # ------------------------------------------------------------ 法規名稱

    def resolve_law(self, name: str) -> tuple[Law | None, list[Law]]:
        """回傳 (確定的法規, 候選清單)。找不到確定的時，候選清單給使用者挑。"""
        q = re.sub(r"\s+", "", name or "")
        if not q:
            return None, []
        if q.upper() in self.laws:
            return self.laws[q.upper()], []
        for cand in (q, q.removeprefix("中華民國"), "中華民國" + q):
            if cand in self.name_index:
                return self.laws[self.name_index[cand]], []
        # 包含關係：輸入是某法規全名的一部分
        subs = [p for n, p in self.name_index.items() if q in n]
        subs = list(dict.fromkeys(subs))
        subs.sort(key=lambda p: (self.laws[p].abolished, len(self.laws[p].name)))
        if len(subs) == 1:
            return self.laws[subs[0]], []
        fuzzy = difflib.get_close_matches(q, self.name_index.keys(), n=8, cutoff=0.5)
        cands = list(dict.fromkeys(subs[:8] + [self.name_index[n] for n in fuzzy]))
        return None, [self.laws[p] for p in cands[:10]]

    def find_laws(self, keyword: str, limit: int = 10, include_abolished: bool = False) -> list[Law]:
        law, cands = self.resolve_law(keyword)
        result = ([law] if law else []) + cands
        if not include_abolished:
            result = [x for x in result if not x.abolished]
        return result[:limit]

    def detect_law_in_query(self, query: str) -> Law | None:
        for n in self._names_by_len:
            if len(n) >= 2 and n in query:
                law = self.laws[self.name_index[n]]
                if not law.abolished:
                    return law
        return None

    # ------------------------------------------------------------ 取條文

    def get_article(self, law_name: str, article: str) -> dict:
        law, cands = self.resolve_law(law_name)
        if law is None:
            return {
                "found": False,
                "reason": f"找不到法規「{law_name}」",
                "candidates": [c.name for c in cands],
            }
        no = normalize_article_no(article)
        if no is None:
            return {"found": False, "reason": f"看不懂條號「{article}」", "law": law.name}
        row = self.conn.execute(
            "SELECT * FROM articles WHERE pcode=? AND art_no=?", (law.pcode, no)
        ).fetchone()
        if row is None:
            nearby = self._nearby_articles(law.pcode, no)
            return {
                "found": False,
                "reason": f"{law.name}沒有{article_display(no)}（可能已刪除或條號有誤）",
                "law": law.name,
                "nearby_articles": nearby,
            }
        return {"found": True, **self._article_dict(row, law)}

    def _nearby_articles(self, pcode: str, no: str) -> list[str]:
        main = int(no.split("-")[0])
        rows = self.conn.execute(
            "SELECT art_no FROM articles WHERE pcode=? AND art_main BETWEEN ? AND ? ORDER BY seq",
            (pcode, main - 2, main + 2),
        ).fetchall()
        return [article_display(r["art_no"]) for r in rows]

    def _article_dict(self, row: sqlite3.Row, law: Law) -> dict:
        return {
            "law": law.name,
            "pcode": law.pcode,
            "nature": law.nature,
            "article": article_display(row["art_no"]),
            "chapter": row["chapter"],
            "text": row["text"].replace("\r\n", "\n"),
            "law_updated": law.updated,
            "abolished": law.abolished,
            "source_url": MOJ_SINGLE.format(pcode=law.pcode, flno=row["art_no"]),
        }

    # ------------------------------------------------------------ 搜尋

    def expand_query(self, query: str) -> tuple[list[str], list[str]]:
        """回傳 (原始詞, 同義詞擴展出的法律用語)。"""
        terms = [t for t in re.split(r"[\s,，、;；]+", query) if t]
        expanded: list[str] = []
        rest = query
        # 長的口語詞先比對，並把比對到的部分遮掉，避免「偷拍」又觸發「偷」
        for key in sorted(self.synonyms, key=len, reverse=True):
            if key in rest:
                rest = rest.replace(key, " ")
                expanded.extend(v for v in self.synonyms[key] if v not in expanded)
        return terms, expanded

    def search(
        self,
        query: str,
        law: str | None = None,
        nature: str | None = None,
        include_abolished: bool = False,
        limit: int = 10,
        mode: str = "auto",
    ) -> dict:
        """mode：auto（有語意向量就用混合，否則關鍵字）、hybrid、keyword、semantic。"""
        limit = max(1, min(int(limit), 50))
        notes: list[str] = []
        if mode not in ("auto", "hybrid", "keyword", "semantic"):
            mode = "auto"
        if mode == "auto":
            mode = "hybrid" if self.vectors is not None else "keyword"
        elif mode in ("hybrid", "semantic") and self.vectors is None:
            notes.append("尚未建立語意向量（build_embeddings.py），這次只用關鍵字搜尋")
            mode = "keyword"

        # 1. 法規範圍：明確指定 > 從查詢裡偵測
        target: Law | None = None
        hard_filter = False
        if law:
            target, cands = self.resolve_law(law)
            if target is None:
                return {"results": [], "notes": [f"找不到法規「{law}」"],
                        "law_candidates": [c.name for c in cands]}
            hard_filter = True
        else:
            target = self.detect_law_in_query(query)
            if target:
                notes.append(f"查詢中提到「{target.name}」，該法規的條文會優先")

        # 2. 數字對照：條文用國字，查詢的「3年」換成「三年」
        q_norm = numbers_to_chinese(query)
        if q_norm != query:
            notes.append(f"數字對照：以「{q_norm}」搜尋")

        # 3. 同義詞擴展
        terms, expanded = self.expand_query(q_norm)
        if expanded:
            notes.append("同義詞擴展：" + "、".join(expanded))
        q_for_tokens = q_norm
        if target and not hard_filter:
            for n in self._names_by_len:
                if n in q_for_tokens and self.name_index[n] == target.pcode:
                    q_for_tokens = q_for_tokens.replace(n, " ")
        base_tokens = list(dict.fromkeys(tokenize(q_for_tokens)))
        syn_tokens = [t for t in dict.fromkeys(tokenize(" ".join(expanded))) if t not in base_tokens]
        all_tokens = base_tokens + syn_tokens
        if not all_tokens:
            return {"results": [], "notes": notes + ["查詢內容太短或只有標點"]}

        # 共用的篩選條件
        filt, fparams = [], []
        if hard_filter and target:
            filt.append("a.pcode = ?")
            fparams.append(target.pcode)
        if nature:
            filt.append("l.nature = ?")
            fparams.append(nature)
        if not include_abolished:
            filt.append("l.abolished = 0")
        filt.append("a.text NOT IN ('（刪除）', '(刪除)')")  # 已刪除的條文不列入

        # 4a. 關鍵字：FTS5 BM25，雙字詞 OR 查詢
        kw_rows: list[sqlite3.Row] = []
        if mode in ("hybrid", "keyword"):
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in all_tokens)
            sql = f"""
                SELECT a.*, bm25(articles_fts, 1.0, 0.6) AS bm
                FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid
                JOIN laws l ON l.pcode = a.pcode
                WHERE articles_fts MATCH ? AND {' AND '.join(filt)}
                ORDER BY bm LIMIT 400
            """
            kw_rows = self.conn.execute(sql, [match] + fparams).fetchall()

        # 4b. 語意：向量相似度（白話描述也找得到，不必剛好用到條文的字）
        sem: dict[int, tuple[int, float]] = {}  # 條文 id → (名次, 相似度)
        if mode in ("hybrid", "semantic"):
            hits = self.vectors.search(q_norm, top_k=600)
            ids = [i for i, _ in hits]
            ok = set()
            for chunk in range(0, len(ids), 500):
                part = ids[chunk:chunk + 500]
                sql = f"""
                    SELECT a.id FROM articles a JOIN laws l ON l.pcode = a.pcode
                    WHERE a.id IN ({','.join('?' * len(part))}) AND {' AND '.join(filt)}
                """
                ok.update(r[0] for r in self.conn.execute(sql, part + fparams))
            for i, sim in hits:
                if i in ok and len(sem) < 300:
                    sem[i] = (len(sem) + 1, sim)

        # 5. 合併候選
        rows: dict[int, sqlite3.Row] = {r["id"]: r for r in kw_rows}
        kw_rank = {r["id"]: n + 1 for n, r in enumerate(kw_rows)}
        missing = [i for i in sem if i not in rows]
        for chunk in range(0, len(missing), 500):
            part = missing[chunk:chunk + 500]
            for r in self.conn.execute(
                f"SELECT a.*, 0.0 AS bm FROM articles a WHERE a.id IN ({','.join('?' * len(part))})", part
            ):
                rows[r["id"]] = r

        # 6. 多層加權重排
        phrases = [t for t in terms if len(t) >= 2] + [e for e in expanded if len(e) >= 2]
        scored = []
        for aid, r in rows.items():
            lw = self.laws[r["pcode"]]
            hay = r["text"] + " " + (r["chapter"] or "")
            hay_tokens = set(tokenize(hay))
            base_cov = sum(t in hay_tokens for t in base_tokens) / max(len(base_tokens), 1)
            syn_cov = sum(t in hay_tokens for t in syn_tokens) / max(len(syn_tokens), 1) if syn_tokens else 0
            coverage = max(base_cov, syn_cov) * 0.7 + min(base_cov, syn_cov) * 0.3 if syn_tokens else base_cov
            phrase_hits = sum(1 for p in phrases if p in hay)
            if mode == "keyword":
                score = -r["bm"]                               # BM25（越大越相關）
                score *= 0.4 + coverage                        # 命中率：查詢詞涵蓋越多越好
                score *= 1 + 0.25 * min(phrase_hits, 4)        # 整詞命中加分
            else:
                # Reciprocal Rank Fusion：兩邊名次都前面的條文分數最高
                k = 60
                score = 0.0
                if aid in kw_rank:
                    score += 1 / (k + kw_rank[aid])
                if aid in sem:
                    score += 1.2 / (k + sem[aid][0])           # 語意略加重
                score *= 100
                score *= 0.7 + 0.5 * coverage
                score *= 1 + 0.15 * min(phrase_hits, 4)
            score *= NATURE_WEIGHT.get(lw.nature, 1.0)         # 法規位階
            score *= POPULAR_LAWS.get(lw.pcode, 1.0)           # 常用法規
            if target and lw.pcode == target.pcode:
                score *= 1.6                                   # 查詢點名的法規
            scored.append((score, r, lw, coverage, phrase_hits))
        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for score, r, lw, cov, ph in scored[:limit]:
            d = self._article_dict(r, lw)
            d["text"] = make_snippet(d["text"], phrases or terms)
            d["score"] = round(score, 3)
            m = {"coverage": round(cov, 2), "phrase_hits": ph}
            if mode != "keyword":
                m["keyword_rank"] = kw_rank.get(r["id"])
                if r["id"] in sem:
                    m["semantic_rank"], sim = sem[r["id"]]
                    m["semantic_similarity"] = round(sim, 3)
            d["match"] = m
            results.append(d)
        if not results:
            notes.append("沒有找到相關條文，可以換個說法或拿掉法規限制再試")
        return {"query": query, "mode": mode, "results": results, "notes": notes}


def make_snippet(text: str, phrases: list[str], width: int = 160) -> str:
    """條文太長時，擷取第一個命中詞附近的片段。"""
    text = text.replace("\r\n", "\n")
    if len(text) <= width * 2:
        return text
    pos = min((text.find(p) for p in phrases if p and p in text), default=0)
    start = max(0, pos - width // 2)
    end = min(len(text), start + width * 2)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
