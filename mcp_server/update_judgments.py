"""下載司法院裁判書開放資料，挑出刑事判決，匯入本機的 judgments.db。

設計成可以每兩週自動跑一次（Windows 工作排程器），重跑不會重複下載或重複匯入：

1. 下載：查詢開放資料平台的「裁判書」資料集，把近 N 個月、還沒下載過的壓縮檔
   存到 --raw-dir（建議是 Google Drive 電腦版的串流資料夾，原始檔只佔雲端空間）。
2. 匯入：--raw-dir 裡還沒匯入過的壓縮檔，解壓到本機暫存資料夾，只挑刑事判決寫進資料庫，
   完成後刪掉暫存檔。原始壓縮檔留在雲端，以後要重建資料庫不必重新下載。

用法：
    python mcp_server/update_judgments.py --raw-dir "G:\\我的雲端硬碟\\判決原始檔"
    python mcp_server/update_judgments.py --raw-dir ... --probe        # 只列出平台上有哪些月份，不下載
    python mcp_server/update_judgments.py --raw-dir ... --no-download  # 只匯入已經在資料夾裡的檔案
    python mcp_server/update_judgments.py --stats                      # 看資料庫現況

也可以自己從瀏覽器下載壓縮檔丟進 --raw-dir，下次執行就會匯入。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judgments import (  # noqa: E402
    INDEX_CHARS, JUDGMENT_DB_PATH, JudgmentDB, insert_judgment, open_for_write, parse_judgment,
)

API = "https://opendata.judicial.gov.tw"
UA = "taiwan-law-mcp-judgment-updater/1.0"
ARCHIVE_EXT = (".rar", ".zip", ".7z")
_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})[-/年]?(0[1-9]|1[0-2])(?!\d)")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def find_month(text: str) -> str | None:
    """從標題或檔名找出 YYYYMM。也接受民國年寫法「112年5月」。"""
    m = _MONTH_RE.search(text or "")
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"(?<!\d)(1[01]\d)\s*年\s*(\d{1,2})\s*月", text or "")
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)) + 1911}{int(m.group(2)):02d}"
    return None


def months_back(n: int, today: dt.date | None = None) -> str:
    """回傳 n 個月前的 YYYYMM（含當月共 n 個月）。"""
    today = today or dt.date.today()
    y, m = today.year, today.month - (n - 1)
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}{m:02d}"


# ---------------------------------------------------------------- 開放資料平台 API

def http_get(url: str, token: str | None = None, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url: str, token: str | None = None):
    with http_get(url, token) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def login() -> str | None:
    """平台若要求會員登入，可用環境變數 JUDICIAL_USER / JUDICIAL_PASSWORD 提供帳密。"""
    user, pwd = os.environ.get("JUDICIAL_USER"), os.environ.get("JUDICIAL_PASSWORD")
    if not (user and pwd):
        return None
    body = json.dumps({"memberAccount": user, "pwd": pwd}).encode()
    req = urllib.request.Request(f"{API}/api/MemberTokens", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8-sig"))
    return data.get("token") or data.get("Token")


def list_remote(token: str | None = None) -> list[dict]:
    """列出平台上的裁判書壓縮檔：[{month, title, fileset_id, format}]。"""
    cats = get_json(f"{API}/data/api/rest/categories", token)
    cat_nos = [c.get("categoryNo") for c in cats if "裁判書" in (c.get("categoryName") or "")]
    if not cat_nos:
        raise RuntimeError("平台的分類清單裡找不到「裁判書」，API 可能改版了")
    out = []
    for no in cat_nos:
        for ds in get_json(f"{API}/data/api/rest/categories/{no}/resources", token):
            title = ds.get("title") or ds.get("datasetName") or ""
            for fs in ds.get("filesets") or []:
                desc = fs.get("resourceDescription") or ""
                month = find_month(desc) or find_month(title)
                if month and fs.get("fileSetId"):
                    out.append({"month": month, "title": title, "description": desc,
                                "fileset_id": fs["fileSetId"],
                                "format": (fs.get("resourceFormat") or "rar").lower()})
    out.sort(key=lambda x: x["month"])
    return out


def download(item: dict, raw_dir: Path, token: str | None = None) -> Path:
    ext = item["format"] if item["format"] in ("rar", "zip", "7z") else "rar"
    dest = raw_dir / f"裁判書_{item['month']}_{item['fileset_id']}.{ext}"
    if dest.exists():
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    url = f"{API}/api/FilesetLists/{item['fileset_id']}/file"
    for attempt in range(4):
        try:
            with http_get(url, token, timeout=300) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, length=1 << 20)
            part.replace(dest)
            return dest
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log(f"  下載失敗（第 {attempt + 1} 次）：{e}")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"下載 {item['month']} 失敗")


# ---------------------------------------------------------------- 解壓與匯入

def iter_json_files(archive: Path, work: Path):
    """逐一產生壓縮檔裡的 (檔名, bytes)。zip 直接串流讀；rar/7z 用 Windows 內建的 tar 解壓。"""
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if info.filename.lower().endswith(".json"):
                    yield info.filename, z.read(info)
        return
    tar = shutil.which("tar") or shutil.which("bsdtar")
    if not tar:
        raise RuntimeError("找不到 tar 指令（Windows 10 以上內建），無法解壓 rar")
    r = subprocess.run([tar, "-xf", str(archive), "-C", str(work)], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"解壓失敗：{r.stderr.decode(errors='replace')[:300]}")
    for p in sorted(work.rglob("*.json")):
        yield str(p.relative_to(work)), p.read_bytes()


def ingest_archive(conn, archive: Path, name_index: dict[str, str], temp_root: Path | None,
                   include_rulings: bool = False, index_chars: int = INDEX_CHARS) -> tuple[int, int]:
    added = scanned = 0
    with tempfile.TemporaryDirectory(prefix="judg_", dir=temp_root) as tmp:
        for i, (name, raw) in enumerate(iter_json_files(archive, Path(tmp)), 1):
            scanned += 1
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            j = parse_judgment(data)
            if j is None or (j.kind == "裁定" and not include_rulings):
                continue
            if insert_judgment(conn, j, name_index, source=archive.name, index_chars=index_chars):
                added += 1
            if i % 5000 == 0:
                conn.commit()
                log(f"  已看過 {i} 份，新增刑事判決 {added} 份")
    return added, scanned


def load_name_index() -> dict[str, str]:
    try:
        from lawsearch import LawDB
        return LawDB().name_index
    except FileNotFoundError:
        log("警告：找不到 laws.db，判決引用的法條不會建索引。請先執行 build_index.py 再重建判決資料庫。")
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下載並匯入司法院刑事判決")
    ap.add_argument("--raw-dir", type=Path, default=os.environ.get("JUDGMENT_RAW_DIR"),
                    help="原始壓縮檔資料夾（建議放 Google Drive 串流資料夾）")
    ap.add_argument("--db", type=Path, default=JUDGMENT_DB_PATH, help="判決資料庫位置")
    ap.add_argument("--months", type=int, default=36, help="只處理最近幾個月，預設 36（3 年）")
    ap.add_argument("--temp-dir", type=Path, default=None, help="解壓用的暫存資料夾（要在本機硬碟）")
    ap.add_argument("--include-rulings", action="store_true", help="連刑事裁定也收（預設只收判決）")
    ap.add_argument("--index-chars", type=int, default=INDEX_CHARS,
                    help=f"每份判決前幾個字進搜尋索引，預設 {INDEX_CHARS}；越大越好找但越佔空間")
    ap.add_argument("--max-new", type=int, default=0, help="這次最多下載幾個月（0 表示不限），第一次試跑可設 1")
    ap.add_argument("--probe", action="store_true", help="只列出平台上有哪些月份，不下載")
    ap.add_argument("--no-download", action="store_true", help="不下載，只匯入資料夾裡已有的檔案")
    ap.add_argument("--stats", action="store_true", help="顯示資料庫現況後結束")
    a = ap.parse_args(argv)

    if a.stats:
        print(json.dumps(JudgmentDB(a.db).stats(), ensure_ascii=False, indent=2))
        size = a.db.stat().st_size / 1e6
        print(f"資料庫大小：{size:.1f} MB")
        return 0

    since = months_back(a.months)
    token = None
    remote: list[dict] = []
    if a.probe or not a.no_download:
        try:
            token = login()
            remote = [x for x in list_remote(token) if x["month"] >= since]
        except Exception as e:  # noqa: BLE001
            log(f"無法讀取開放資料平台：{e}")
            log("可以改從 https://opendata.judicial.gov.tw/ 用瀏覽器下載裁判書壓縮檔放進 --raw-dir，"
                "再加 --no-download 執行。")
            if a.probe:
                return 1
            remote = []
        if a.probe:
            for x in remote:
                print(f"{x['month']}  {x['format']}  fileset={x['fileset_id']}  {x['title']}")
            print(f"共 {len(remote)} 個檔案（{since} 之後）")
            return 0

    if not a.raw_dir:
        ap.error("請用 --raw-dir 指定原始檔資料夾（或設定環境變數 JUDGMENT_RAW_DIR）")
    a.raw_dir.mkdir(parents=True, exist_ok=True)

    conn = open_for_write(a.db)
    done = {r[0] for r in conn.execute("SELECT archive FROM ingested")}

    # 1. 下載缺的月份
    new_count = 0
    for item in remote:
        if any(f"_{item['month']}_{item['fileset_id']}." in d for d in done):
            continue
        existing = list(a.raw_dir.glob(f"裁判書_{item['month']}_{item['fileset_id']}.*"))
        if existing and not existing[0].name.endswith(".part"):
            continue
        if a.max_new and new_count >= a.max_new:
            log(f"已達 --max-new {a.max_new}，其餘下次再下載")
            break
        log(f"下載 {item['month']} …")
        path = download(item, a.raw_dir, token)
        log(f"  完成 {path.name}（{path.stat().st_size / 1e6:.0f} MB）")
        new_count += 1

    # 2. 匯入還沒處理的壓縮檔
    name_index = load_name_index()
    archives = sorted(p for p in a.raw_dir.iterdir()
                      if p.suffix.lower() in ARCHIVE_EXT and p.name not in done)
    archives = [p for p in archives if (find_month(p.name) or "999999") >= since]
    if not archives:
        log("沒有新的檔案要匯入，資料庫已是最新")
    for p in archives:
        log(f"匯入 {p.name} …")
        t0 = time.time()
        try:
            added, scanned = ingest_archive(conn, p, name_index, a.temp_dir,
                                            a.include_rulings, a.index_chars)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            log(f"  失敗，下次再試：{e}")
            continue
        conn.execute("INSERT OR REPLACE INTO ingested VALUES (?,?,?,?,?)",
                     (p.name, find_month(p.name), added, scanned, dt.datetime.now().isoformat()))
        conn.commit()
        log(f"  看過 {scanned} 份，新增刑事判決 {added} 份（{time.time() - t0:.0f} 秒）")

    if archives:
        conn.execute("INSERT INTO judgments_fts(judgments_fts) VALUES ('optimize')")
        conn.commit()
    conn.close()
    log(f"完成，資料庫大小 {a.db.stat().st_size / 1e6:.1f} MB：{a.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
