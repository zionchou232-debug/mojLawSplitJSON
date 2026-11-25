"""執行：python -m pytest mcp_server/tests（需先 build_index.py）"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lawsearch import LawDB, normalize_article_no  # noqa: E402


@pytest.fixture(scope="module")
def db():
    return LawDB()


@pytest.mark.parametrize("raw,want", [
    ("271", "271"), ("第271條", "271"), ("第 185-3 條", "185-3"), ("185之3", "185-3"),
    ("第一百八十五條之三", "185-3"), ("二七一", "271"), ("十", "10"), ("２７１", "271"),
    ("abc", None),
])
def test_normalize_article_no(raw, want):
    assert normalize_article_no(raw) == want


@pytest.mark.parametrize("law,art,name", [
    ("刑法", "271", "中華民國刑法"), ("刑訴", "93", "刑事訴訟法"),
    ("跟騷法", "18", "跟蹤騷擾防制法"), ("C0000001", "185之4", "中華民國刑法"),
])
def test_get_article(db, law, art, name):
    r = db.get_article(law, art)
    assert r["found"] and r["law"] == name and r["text"]
    assert r["source_url"].startswith("https://law.moj.gov.tw/")


def test_get_article_missing_does_not_invent(db):
    r = db.get_article("刑法", "999")
    assert r["found"] is False and "text" not in r


@pytest.mark.parametrize("query,law,art", [
    ("酒駕撞死人", "中華民國刑法", "第185條之3"),
    ("偷東西", "中華民國刑法", "第320條"),
    ("正當防衛", "中華民國刑法", "第23條"),
    ("偷拍", "中華民國刑法", "第319條之1"),
])
def test_search_top5(db, query, law, art):
    hits = [(x["law"], x["article"]) for x in db.search(query, limit=5)["results"]]
    assert (law, art) in hits


def test_search_with_law_filter(db):
    res = db.search("羈押 期間", law="刑訴", limit=5)["results"]
    assert res and all(x["law"] == "刑事訴訟法" for x in res)


def test_search_skips_deleted_articles(db):
    assert all(x["text"] != "（刪除）" for x in db.search("竊盜", limit=30)["results"])
