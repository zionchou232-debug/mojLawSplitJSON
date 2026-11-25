"""比較不同搜尋模式的效果：每個查詢都有「應該找到的條文」，看前 5 名有沒有它。

用法：python mcp_server/eval_search.py
可以自己在 CASES 加題目，越貼近你實際會問的越好。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lawsearch import LawDB  # noqa: E402

# (查詢, 應該找到的法規, 條號)
CASES = [
    ("酒駕撞死人", "中華民國刑法", "第185條之3"),
    ("撞到人就跑掉", "中華民國刑法", "第185條之4"),
    ("偷東西", "中華民國刑法", "第320條"),
    ("拿別人寄放的錢自己花掉", "中華民國刑法", "第335條"),
    ("在網路上罵人", "中華民國刑法", "第309條"),
    ("說別人壞話而且是假的", "中華民國刑法", "第310條"),
    ("被打的時候還手", "中華民國刑法", "第23條"),
    ("小孩子犯罪會不會被關", "中華民國刑法", "第18條"),
    ("打架把人打成重傷", "中華民國刑法", "第278條"),
    ("偷拍別人洗澡", "中華民國刑法", "第319條之1"),
    ("騙人匯款", "中華民國刑法", "第339條"),
    ("把帳戶借給詐騙集團", "洗錢防制法", "第22條"),
    ("被關多久可以申請提早出來", "中華民國刑法", "第77條"),
    ("判6個月以下可以換成罰錢", "中華民國刑法", "第41條"),
    ("犯罪後主動去警察局承認", "中華民國刑法", "第62條"),
    ("警察可以關嫌犯多久才要送法院", "刑事訴訟法", "第93條"),
    ("被告可以不回答問題", "刑事訴訟法", "第95條"),
    ("沒有搜索票可以搜索嗎", "刑事訴訟法", "第131條"),
    ("檢察官決定不起訴", "刑事訴訟法", "第252條"),
    ("一直跟蹤騷擾前女友", "跟蹤騷擾防制法", "第18條"),
]


def run(db: LawDB, mode: str, k: int = 5) -> tuple[int, list[str]]:
    hits, misses = 0, []
    for q, law, art in CASES:
        res = db.search(q, mode=mode, limit=k)["results"]
        found = [(x["law"], x["article"]) for x in res]
        if (law, art) in found:
            hits += 1
        else:
            misses.append(f"{q} → 想找 {law}{art}，前幾名是 " + "、".join(f"{a}{b}" for a, b in found[:3]))
    return hits, misses


def main() -> None:
    db = LawDB()
    modes = ["keyword"] + (["semantic", "hybrid"] if db.vectors is not None else [])
    if db.vectors is None:
        print("（沒有語意向量，只測關鍵字。先跑 build_embeddings.py）")
    for mode in modes:
        hits, misses = run(db, mode)
        print(f"\n【{mode}】前 5 名命中 {hits}/{len(CASES)}")
        for m in misses:
            print("  ✗", m)


if __name__ == "__main__":
    main()
