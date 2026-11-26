"""判決功能測試：用假的裁判書 JSON 打包成 zip，走一次匯入流程再查詢。

執行：python -m pytest mcp_server/tests（需先 build_index.py 產生 laws.db）
"""
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_judgments  # noqa: E402
from judgments import (  # noqa: E402
    JudgmentDB, classify, extract_citations, parse_case_number, parse_judgment,
)
from lawsearch import LawDB  # noqa: E402

SUPREME = """最高法院刑事判決
112年度台上字第1234號
上  訴  人  王○○
主  文
上訴駁回。
理  由
一、原審認定上訴人以詐術使人交付財物，係犯刑法第339條之4第1項第2款之三人以上共同犯詐欺取財罪，
並依洗錢防制法第十九條處斷。被告提領車手款項……
"""

HIGH = """臺灣高等法院臺中分院刑事判決
113年度上訴字第56號
上  訴  人  臺灣臺中地方檢察署檢察官
主  文
原判決撤銷。
被告犯不能安全駕駛致人於死罪，處有期徒刑貳年。
理  由
一、被告酒後駕車，吐氣所含酒精濃度達每公升0.55毫克，撞死行人，犯刑法第一百八十五條之三第二項前段之罪。
"""

DISTRICT_RULING = """臺灣臺北地方法院刑事裁定
113年度聲字第7號
主  文
聲請駁回。
"""

CIVIL = """臺灣臺北地方法院民事判決
113年度訴字第8號
主  文
被告應給付原告新臺幣十萬元。理由：依民法第184條……
"""

INCIDENTAL_CIVIL = """臺灣臺北地方法院刑事附帶民事訴訟判決
113年度附民字第9號
主  文
原告之訴駁回。
"""

DOCS = [
    {"JID": "TPSM,112,台上,1234,20230510,1", "JYEAR": "112", "JCASE": "台上", "JNO": "1234",
     "JDATE": "20230510", "JTITLE": "加重詐欺等", "JFULL": SUPREME},
    {"JID": "TCHM,113,上訴,56,20240320,1", "JYEAR": "113", "JCASE": "上訴", "JNO": "56",
     "JDATE": "20240320", "JTITLE": "公共危險", "JFULL": HIGH},
    {"JID": "TPDM,113,聲,7,20240101,1", "JYEAR": "113", "JCASE": "聲", "JNO": "7",
     "JDATE": "20240101", "JTITLE": "聲請", "JFULL": DISTRICT_RULING},
    {"JID": "TPDV,113,訴,8,20240102,1", "JYEAR": "113", "JCASE": "訴", "JNO": "8",
     "JDATE": "20240102", "JTITLE": "損害賠償", "JFULL": CIVIL},
    {"JID": "TPDM,113,附民,9,20240103,1", "JYEAR": "113", "JCASE": "附民", "JNO": "9",
     "JDATE": "20240103", "JTITLE": "損害賠償", "JFULL": INCIDENTAL_CIVIL},
]


@pytest.fixture(scope="module")
def law_db():
    return LawDB()


@pytest.fixture(scope="module")
def jdb(tmp_path_factory, law_db):
    tmp = tmp_path_factory.mktemp("j")
    raw = tmp / "raw"
    raw.mkdir()
    with zipfile.ZipFile(raw / "裁判書_202405_1.zip", "w") as z:
        for i, d in enumerate(DOCS):
            z.writestr(f"202405/x/{i}.json", json.dumps(d, ensure_ascii=False).encode("utf-8-sig"))
    db_path = tmp / "judgments.db"
    args = ["--raw-dir", str(raw), "--db", str(db_path), "--no-download", "--months", "1200"]
    assert update_judgments.main(args) == 0
    assert update_judgments.main(args) == 0  # 重跑不會重複匯入
    return JudgmentDB(db_path, law_db=law_db)


def test_classify():
    assert classify(SUPREME) == "判決"
    assert classify(DISTRICT_RULING) == "裁定"
    assert classify(CIVIL) is None
    assert classify(INCIDENTAL_CIVIL) is None
    assert classify("臺灣臺北地方法院刑事簡易判決\n113年度簡字第1號") == "判決"


def test_parse_judgment_court():
    assert parse_judgment(DOCS[0]).court == "最高法院"
    j = parse_judgment(DOCS[1])
    assert j.court == "臺灣高等法院臺中分院" and j.level == "高等法院"
    assert j.main_text.startswith("原判決撤銷")


@pytest.mark.parametrize("raw,want", [
    ("最高法院112年度台上字第1234號", (112, "台上", 1234)),
    ("最高法院 112 年度 臺上 字第 1234 號刑事判決", (112, "臺上", 1234)),
    ("112台上1234", (112, "台上", 1234)),
    ("113年度上訴字第56號", (113, "上訴", 56)),
    ("１１３年度交易字第２號", (113, "交易", 2)),
])
def test_parse_case_number(raw, want):
    q = parse_case_number(raw)
    assert (q["year"], q["word"], q["no"]) == want


def test_extract_citations(law_db):
    c = extract_citations(SUPREME + HIGH, law_db.name_index)
    assert ("C0000001", "339-4") in c
    assert ("C0000001", "185-3") in c
    assert any(no == "19" for _, no in c)  # 洗錢防制法第十九條
    # 「陸海空軍刑法」不能被當成「刑法」
    c2 = extract_citations("依陸海空軍刑法第44條", law_db.name_index)
    assert ("C0000001", "44") not in c2


def test_only_criminal_judgments_imported(jdb):
    s = jdb.stats()
    assert s["judgments"] == 2
    assert s["months"] == ["202405"]


def test_get_judgment(jdb):
    r = jdb.get("最高法院112年度臺上字第1234號")
    assert r["found"] and r["court"] == "最高法院"
    assert "三人以上共同犯詐欺取財罪" in r["full_text"]
    assert r["source_url"].startswith("https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=TPSM")
    assert jdb.get("112台上1234")["found"]
    assert not jdb.get("113年度聲字第7號")["found"]  # 裁定預設不收
    r = jdb.get("112台上1234", max_chars=1000, offset=0)
    assert r["total_chars"] == len(SUPREME)


def test_search_judgments(jdb):
    r = jdb.search("酒駕 撞死")
    assert r["results"] and r["results"][0]["jid"].startswith("TCHM")
    r = jdb.search("詐欺 車手", level="最高法院")
    assert [x["jid"][:4] for x in r["results"]] == ["TPSM"]
    assert jdb.search("詐欺", year_from=113)["results"] == []


def test_find_by_article(jdb):
    r = jdb.by_article("刑法", "339之4")
    assert r["total"] == 1 and r["results"][0]["jid"].startswith("TPSM")
    r = jdb.by_article("刑法", "第一百八十五條之三")
    assert r["total"] == 1
    assert jdb.by_article("刑法", "271")["total"] == 0


@pytest.mark.parametrize("text,want", [
    ("裁判書202301.rar", "202301"), ("2023-05 裁判書", "202305"), ("112年5月裁判書", "202305"),
    ("nothing", None),
])
def test_find_month(text, want):
    assert update_judgments.find_month(text) == want


# ---------------------------------------------------------------- 語意向量（用假模型測流程）

import zlib  # noqa: E402

import numpy as np  # noqa: E402

from judgment_vectors import JudgmentVectors, save_month  # noqa: E402
from judgments import fact_passage  # noqa: E402
from lawsearch import tokenize  # noqa: E402


class BigramEmbedder:
    """測試用：雙字詞雜湊成 256 維，意思相近就是用字相近。"""
    name = "test-bigram"

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(256, dtype=np.float32)
        for t in tokenize(text):
            v[zlib.crc32(t.encode()) % 256] += 1
        return v / max(np.linalg.norm(v), 1e-9)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed(text)


def test_fact_passage_skips_header():
    p = fact_passage(HIGH, "公共危險")
    assert p.startswith("公共危險：理由") and "酒後駕車" in p and "上訴人" not in p


@pytest.fixture(scope="module")
def jdb_vec(jdb, tmp_path_factory):
    emb = BigramEmbedder()
    out = tmp_path_factory.mktemp("vec")
    docs = [d for d in DOCS if parse_judgment(d) and parse_judgment(d).kind == "判決"]
    vecs = np.stack([emb.embed(fact_passage(d["JFULL"], d["JTITLE"])) for d in docs])
    save_month(out, "裁判書_202405_1", [d["JID"] for d in docs], vecs, emb.name)
    assert not list(out.glob("*.tmp*"))
    jdb.vectors = JudgmentVectors(out, embedder=emb)
    yield jdb
    jdb.vectors = None


def test_semantic_search(jdb_vec):
    assert jdb_vec.vectors.count == 2
    r = jdb_vec.search("酒後駕車撞死行人", mode="semantic")
    assert r["mode"] == "semantic" and r["results"][0]["jid"].startswith("TCHM")
    r = jdb_vec.search("詐欺 車手")
    assert r["mode"] == "hybrid" and r["results"][0]["jid"].startswith("TPSM")
    assert "keyword_rank" in r["results"][0]["match"]


def test_similar_judgments(jdb_vec):
    r = jdb_vec.similar("112台上1234")
    assert [x["jid"][:4] for x in r["results"]] == ["TCHM"]


def test_semantic_falls_back_without_vectors(jdb):
    saved, jdb.vectors = jdb.vectors, None
    try:
        r = jdb.search("酒駕", mode="semantic")
        assert r["mode"] == "keyword" and r["notes"]
    finally:
        jdb.vectors = saved
