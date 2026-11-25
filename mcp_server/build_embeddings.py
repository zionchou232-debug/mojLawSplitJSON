"""替條文建立語意向量（需先跑過 build_index.py）。

用法：
  pip install -r mcp_server/requirements-semantic.txt
  python mcp_server/build_embeddings.py              # 法律＋憲法層級，約 4 萬條
  python mcp_server/build_embeddings.py --all        # 連命令層級一起，約 16 萬條，較久
  python mcp_server/build_embeddings.py --limit 500  # 先試跑 500 條，確認環境正常
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lawsearch import DB_PATH, article_display  # noqa: E402
from semantic import DEFAULT_MODEL, FastEmbedder, VectorIndex, article_passage, vector_path  # noqa: E402


def select_articles(conn: sqlite3.Connection, include_orders: bool, limit: int | None):
    natures = ("憲法", "法律", "命令") if include_orders else ("憲法", "法律")
    sql = f"""
        SELECT a.id, a.art_no, a.chapter, a.text, l.name
        FROM articles a JOIN laws l ON l.pcode = a.pcode
        WHERE l.abolished = 0 AND a.text NOT IN ('（刪除）', '(刪除)')
          AND l.nature IN ({','.join('?' * len(natures))})
        ORDER BY a.id
    """
    rows = conn.execute(sql, natures).fetchall()
    return rows[:limit] if limit else rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="包含命令層級（行政規則、辦法等）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=None, help="只處理前 N 條（試跑用）")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    rows = select_articles(conn, args.all, args.limit)
    print(f"共 {len(rows)} 條條文，模型 {args.model}（第一次會先下載模型）")
    embedder = FastEmbedder(args.model)

    t0 = time.time()
    ids, chunks = [], []
    step = 2000
    for i in range(0, len(rows), step):
        part = rows[i : i + step]
        texts = [article_passage(r[4], r[2] or "", article_display(r[1]), r[3]) for r in part]
        chunks.append(embedder.embed_documents(texts, batch_size=args.batch))
        ids.extend(r[0] for r in part)
        done = i + len(part)
        rate = done / max(time.time() - t0, 1e-6)
        print(f"  {done}/{len(rows)}，剩約 {(len(rows) - done) / rate / 60:.1f} 分鐘", flush=True)

    index = VectorIndex(np.array(ids), np.vstack(chunks), args.model)
    out = vector_path(args.model)
    index.save(out, {"scope": "all" if args.all else "憲法+法律"})
    print(f"完成：{len(ids)} 條 → {out}（{(time.time() - t0) / 60:.1f} 分鐘）")


if __name__ == "__main__":
    main()
