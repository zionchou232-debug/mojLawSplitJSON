"""語意搜尋與數字對照的測試。用假的 embedder，不需要下載模型。"""
import hashlib
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lawsearch import DB_PATH, LawDB, int_to_chinese, numbers_to_chinese, tokenize  # noqa: E402
from semantic import VectorIndex  # noqa: E402

DIM = 256


class FakeEmbedder:
    """把雙字詞雜湊到固定維度，當作「向量」。只用來測流程，不代表真實語意。"""
    name = "fake"

    def _vec(self, text):
        v = np.zeros(DIM, dtype=np.float32)
        for t in tokenize(text):
            v[int(hashlib.md5(t.encode()).hexdigest(), 16) % DIM] += 1
        return v / max(np.linalg.norm(v), 1e-9)

    def embed_documents(self, texts):
        return np.vstack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture(scope="module")
def db():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text FROM articles WHERE pcode IN ('C0000001','C0010001')"
    ).fetchall()
    emb = FakeEmbedder()
    index = VectorIndex(np.array([r[0] for r in rows]), emb.embed_documents([r[1] for r in rows]),
                        "fake", embedder=emb)
    return LawDB(vectors=index)


@pytest.mark.parametrize("n,want", [
    (3, "三"), (10, "十"), (15, "十五"), (110, "一百十"), (105, "一百零五"),
    (30000, "三萬"), (300000, "三十萬"), (2000000, "二百萬"),
])
def test_int_to_chinese(n, want):
    assert int_to_chinese(n) == want


def test_numbers_to_chinese():
    assert numbers_to_chinese("3年以下 罰30萬 0.25毫克") == "三年以下 罰三十萬 零點二五毫克"


def test_arabic_number_query_matches_chinese_text():
    db = LawDB(load_vectors=False)
    res = db.search("殺人 10年以上", law="刑法", limit=3)
    assert any(x["article"] == "第271條" for x in res["results"])
    assert any("數字對照" in n for n in res["notes"])


def test_hybrid_mode_used_when_vectors_exist(db):
    res = db.search("酒駕撞死人", limit=5)
    assert res["mode"] == "hybrid"
    top = [(x["law"], x["article"]) for x in res["results"]]
    assert ("中華民國刑法", "第185條之3") in top
    assert "semantic_rank" in res["results"][0]["match"] or res["results"][0]["match"]["keyword_rank"]


def test_semantic_only_respects_law_filter(db):
    res = db.search("羈押", law="刑訴", mode="semantic", limit=5)
    assert res["results"] and all(x["law"] == "刑事訴訟法" for x in res["results"])


def test_falls_back_to_keyword_without_vectors():
    db = LawDB(load_vectors=False)
    res = db.search("偷東西", mode="hybrid", limit=3)
    assert res["mode"] == "keyword"
    assert any("語意向量" in n for n in res["notes"])


def test_vector_index_roundtrip(tmp_path):
    emb = FakeEmbedder()
    idx = VectorIndex(np.array([5, 7]), emb.embed_documents(["竊取他人動產", "殺人者處死刑"]), "fake", emb)
    p = tmp_path / "v.npz"
    idx.save(p)
    back = VectorIndex.load(p, embedder=emb)
    assert back.search("殺人", top_k=1)[0][0] == 7
