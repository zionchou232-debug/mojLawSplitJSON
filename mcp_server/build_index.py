"""把 FalVMingLing/*.json 建成 SQLite + FTS5 索引（mcp_server/laws.db）。

用法：python mcp_server/build_index.py
資料更新（git pull 上游）後重跑一次即可，約需 1 分鐘。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lawsearch import DATA_DIR, DB_PATH, EXTRA_ALIASES, article_label_to_no, tokenize  # noqa: E402


def build(data_dir: Path = DATA_DIR, db_path: Path = DB_PATH) -> None:
    t0 = time.time()
    src = data_dir / "FalVMingLing"
    if not src.is_dir():
        sys.exit(f"找不到資料夾 {src}")
    aliases = json.loads((data_dir / "aliases.json").read_text(encoding="utf-8"))
    for pcode, extra in EXTRA_ALIASES.items():
        aliases.setdefault(pcode, [])
        aliases[pcode] += [a for a in extra if a not in aliases[pcode]]

    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(
        """
        CREATE TABLE laws (
            pcode TEXT PRIMARY KEY, name TEXT, nature TEXT, category TEXT,
            url TEXT, updated TEXT, abolished INTEGER, aliases TEXT
        );
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY, pcode TEXT, seq INTEGER, art_no TEXT,
            art_main INTEGER, chapter TEXT, text TEXT
        );
        CREATE INDEX idx_articles ON articles(pcode, art_no);
        CREATE INDEX idx_articles_main ON articles(pcode, art_main);
        -- 內容由 Python 先切成雙字詞、以空白分隔，FTS5 只負責 BM25
        CREATE VIRTUAL TABLE articles_fts USING fts5(
            body, chapter, content='', tokenize='unicode61'
        );
        """
    )
    n_laws = n_arts = 0
    for f in sorted(src.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        pcode = f.stem
        name = d["法規名稱"]
        names = [name] + aliases.get(pcode, [])
        if name.startswith("中華民國"):
            names.append(name.removeprefix("中華民國"))
        conn.execute(
            "INSERT INTO laws VALUES (?,?,?,?,?,?,?,?)",
            (pcode, name, d.get("法規性質", ""), d.get("法規類別", ""), d.get("法規網址", ""),
             d.get("最新異動日期", ""), 1 if d.get("廢止註記") else 0,
             json.dumps(list(dict.fromkeys(names)), ensure_ascii=False)),
        )
        n_laws += 1
        chapter = ""
        for seq, item in enumerate(d.get("法規內容", [])):
            if "編章節" in item:
                chapter = item["編章節"].strip()
                continue
            if "條號" not in item:
                continue
            no = article_label_to_no(item["條號"])
            text = item.get("條文內容", "")
            try:
                main = int(no.split("-")[0])
            except ValueError:
                main = -1
            cur = conn.execute(
                "INSERT INTO articles (pcode, seq, art_no, art_main, chapter, text) VALUES (?,?,?,?,?,?)",
                (pcode, seq, no, main, chapter, text),
            )
            conn.execute(
                "INSERT INTO articles_fts (rowid, body, chapter) VALUES (?,?,?)",
                (cur.lastrowid, " ".join(tokenize(text)), " ".join(tokenize(chapter))),
            )
            n_arts += 1
    conn.execute("INSERT INTO articles_fts(articles_fts) VALUES ('optimize')")
    conn.commit()
    conn.close()
    tmp.replace(db_path)
    print(f"完成：{n_laws} 部法規、{n_arts} 條條文 → {db_path}（{time.time() - t0:.0f} 秒）")


if __name__ == "__main__":
    build()
