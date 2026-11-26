"""罪名卡測試。執行：python -m pytest mcp_server/tests/test_crimes.py"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crimes import CRIMES_DIR, ELEMENT_TYPES, CrimeBook  # noqa: E402
from lawsearch import DB_PATH, LawDB  # noqa: E402

book = CrimeBook.load()


def test_cards_loaded():
    assert len(book.cards) >= 6
    assert len(list(CRIMES_DIR.glob("*.json"))) == len(book.cards)


@pytest.mark.parametrize("cid", sorted(book.cards))
def test_card_schema(cid):
    c = book.cards[cid]
    for key in ("id", "title", "aliases", "status", "articles", "elements"):
        assert key in c, key
    assert c["status"] in ("draft", "reviewed")
    assert c["articles"] and c["elements"]
    assert any(e["type"] == "主觀" for e in c["elements"])
    for e in c["elements"]:
        assert e["type"] in ELEMENT_TYPES
        assert e["name"] and e["evidence"]
    for p in c.get("precedents", []):
        assert p.get("case") and "verified" in p


@pytest.mark.skipif(not DB_PATH.exists(), reason="需先 build_index.py")
@pytest.mark.parametrize("cid", sorted(book.cards))
def test_card_articles_exist(cid):
    r = book.get(cid, law_db=LawDB())
    missing = [f'{a["law"]} {a["article"]}' for a in r["articles"] if not a.get("text")]
    assert not missing


@pytest.mark.parametrize("q,cid", [
    ("酒駕", "drunk_driving"), ("185之3", "drunk_driving"), ("第三百三十九條之四", "aggravated_fraud"),
    ("車手", "aggravated_fraud"), ("人頭帳戶", "account_aid"), ("吸毒", "drug_use"),
    ("販毒", "drug_sale"), ("320", "theft"), ("加重竊盜", "theft"),
])
def test_resolve(q, cid):
    r = book.get(q, include_article_text=False)
    assert r["found"] and r["id"] == cid


def test_ambiguous_and_missing():
    r = book.get("詐欺", include_article_text=False)
    assert r["found"] is False and len(r["candidates"]) >= 2
    r = book.get("殺人", include_article_text=False)
    assert r["found"] is False and r["available"]


def test_checklist_markdown():
    r = book.checklist("酒駕")
    md = r["markdown"]
    assert md.startswith("# 蒐證檢核清單") and "- [ ] 呼氣酒精測試紀錄" in md
    assert "## 起訴前確認" in md and "草稿" in md
    assert "markdown" not in book.checklist("酒駕", fmt="json")
