"""罪名卡：各罪構成要件拆解與蒐證檢核清單。

每個罪名一個 JSON 檔，放在 mcp_server/crimes/。內容由 Claude 起草、檢察官審閱，
status 為 "draft" 表示尚未審閱，"reviewed" 表示已審。
條文原文不寫在卡片裡，查詢時從法規資料庫（laws.db）即時取出，避免條文修正後卡片過時。

偵查不公開：這裡的工具只接受罪名、法條，不接受案情。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CRIMES_DIR = Path(__file__).resolve().parent / "crimes"

ELEMENT_TYPES = ("客觀", "主觀", "加重", "處罰條件")
STATUS_NOTE = {
    "draft": "這張罪名卡是 Claude 起草、尚未經檢察官審閱的草稿，引用前請自行確認。",
    "reviewed": "",
}


def _norm(s: str) -> str:
    from lawsearch import normalize_article_no
    art = normalize_article_no(s)
    if art:
        return art
    s = s.strip().replace("臺", "台").replace(" ", "")
    s = s.replace("條之", "-").replace("之", "-")
    s = re.sub(r"^第|條$", "", s)
    return s.lower()


@dataclass
class CrimeBook:
    cards: dict[str, dict]

    @classmethod
    def load(cls, directory: Path = CRIMES_DIR) -> "CrimeBook":
        cards = {}
        for p in sorted(directory.glob("*.json")):
            card = json.loads(p.read_text(encoding="utf-8"))
            cards[card["id"]] = card
        return cls(cards)

    def list(self) -> list[dict]:
        return [
            {"id": c["id"], "title": c["title"], "aliases": c.get("aliases", []),
             "status": c.get("status", "draft"),
             "articles": [f'{a["law"]} {a["article"]}' for a in c.get("articles", [])[:3]]}
            for c in self.cards.values()
        ]

    def resolve(self, crime: str) -> list[dict]:
        """依 id、罪名、簡稱或條號找罪名卡。完全符合優先，再來是部分符合。"""
        q = _norm(crime)
        if not q:
            return []
        exact, partial = [], []
        for c in self.cards.values():
            names = [c["id"], c["title"], *c.get("aliases", [])]
            normed = [_norm(n) for n in names]
            if q in normed:
                exact.append(c)
            elif any(q in n or (len(n) >= 2 and n in q) for n in normed):
                partial.append(c)
        return exact or partial

    def get(self, crime: str, law_db=None, include_article_text: bool = True) -> dict:
        found = self.resolve(crime)
        if not found:
            return {"found": False, "reason": f"沒有「{crime}」的罪名卡。",
                    "available": [{"id": c["id"], "title": c["title"]} for c in self.cards.values()]}
        if len(found) > 1:
            return {"found": False, "reason": f"「{crime}」符合多張罪名卡，請指定其中一個 id。",
                    "candidates": [{"id": c["id"], "title": c["title"]} for c in found]}
        card = json.loads(json.dumps(found[0]))  # deep copy
        notes = [n for n in [STATUS_NOTE.get(card.get("status", "draft"), "")] if n]
        if include_article_text:
            if law_db is None:
                notes.append("沒有法規資料庫，未附條文原文；請先執行 build_index.py。")
            else:
                for a in card.get("articles", []):
                    r = law_db.get_article(a["law"], a["article"])
                    if r.get("found"):
                        a["text"] = r["text"]
                        a["source_url"] = r.get("source_url")
                        a["law"] = r.get("law", a["law"])
                    else:
                        a["text"] = None
                        notes.append(f'找不到 {a["law"]} 第{a["article"]}條，請檢查罪名卡。')
        card["found"] = True
        card["notes"] = notes
        return card

    def checklist(self, crime: str, fmt: str = "markdown") -> dict:
        card = self.get(crime, include_article_text=False)
        if not card.get("found"):
            return card
        groups = []
        for e in card.get("elements", []):
            groups.append({"element": f'【{e["type"]}】{e["name"]}',
                           "evidence": e.get("evidence", []), "issues": e.get("issues", [])})
        result = {
            "found": True, "id": card["id"], "title": card["title"], "status": card.get("status"),
            "groups": groups, "procedure_checks": card.get("procedure_checks", []),
            "notes": card["notes"],
        }
        if fmt == "markdown":
            result["markdown"] = render_checklist_markdown(card)
        return result


def render_checklist_markdown(card: dict) -> str:
    lines = [f'# 蒐證檢核清單：{card["title"]}', ""]
    arts = "、".join(f'{a["law"]}第{a["article"]}條' for a in card.get("articles", [])[:4])
    if arts:
        lines += [f"相關條文：{arts}", ""]
    if card.get("status", "draft") == "draft":
        lines += ["> 草稿，尚未經檢察官審閱。", ""]
    for e in card.get("elements", []):
        lines.append(f'## 【{e["type"]}】{e["name"]}')
        lines += [f"- [ ] {x}" for x in e.get("evidence", [])]
        if e.get("issues"):
            lines.append("")
            lines.append("常見爭點：")
            lines += [f"- {x}" for x in e["issues"]]
        lines.append("")
    if card.get("procedure_checks"):
        lines.append("## 起訴前確認")
        lines += [f"- [ ] {x}" for x in card["procedure_checks"]]
        lines.append("")
    if card.get("forfeiture"):
        lines.append("## 沒收")
        lines += [f"- [ ] {x}" for x in card["forfeiture"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
