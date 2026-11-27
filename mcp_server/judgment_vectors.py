"""判決的語意向量：讀 Colab 筆記本算好的向量檔，查詢時找「事實最接近」的判決。

向量檔由 colab/judgment_embeddings.ipynb 產生，每個月一組，放在 Google Drive 的「判決向量」資料夾：
    <月包檔名>.vec.npy   float16，形狀 (判決數, 維度)，已正規化
    <月包檔名>.jid.json  {"model": ..., "jids": [...]}，跟向量一列一列對應
Google Drive 電腦版會把這個資料夾同步成本機路徑（例如 G:\\我的雲端硬碟\\判決向量），
設定環境變數 JUDGMENT_VECTOR_DIR 指過去就能用，不必手動下載。

查詢時要用跟 Colab 一樣的模型把查詢轉成向量。預設 BAAI/bge-m3：
- 優先用 fastembed 載入官方 ONNX 檔（不用裝 PyTorch，第一次會下載約 2.3GB 到 mcp_server/models/）
- 沒有 fastembed 時改用 sentence-transformers
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

HERE = Path(__file__).resolve().parent
JUDGMENT_VECTOR_DIR = Path(os.environ.get("JUDGMENT_VECTOR_DIR", HERE / "judgment_vectors"))
JUDGMENT_EMBED_MODEL = os.environ.get("JUDGMENT_EMBED_MODEL", "BAAI/bge-m3")
# 模型檔放固定位置；fastembed 預設放系統暫存資料夾，可能被 Windows 磁碟清理刪掉而要重新下載 2.3GB
MODEL_CACHE_DIR = Path(os.environ.get("FASTEMBED_CACHE_PATH", HERE / "models"))
CHUNK = 50_000  # 一次拿多少列轉成 float32 做矩陣乘法，控制記憶體


class Embedder(Protocol):
    name: str

    def embed_query(self, text: str) -> np.ndarray: ...


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)


class FastEmbedBgeM3:
    """用 fastembed 跑 BAAI/bge-m3 官方 ONNX（dense 向量 = CLS + 正規化）。"""

    def __init__(self, model: str = "BAAI/bge-m3"):
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        try:
            TextEmbedding.add_custom_model(
                model=model, pooling=PoolingType.CLS, normalization=True,
                sources=ModelSource(hf=model), dim=1024,
                model_file="onnx/model.onnx", additional_files=["onnx/model.onnx_data"],
            )
        except ValueError:
            pass  # 已經註冊過
        self.name = model
        self._model = TextEmbedding(model_name=model, cache_dir=str(MODEL_CACHE_DIR))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(np.array(list(self._model.embed([text])))[0])


class SentenceTransformerEmbedder:
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer

        self.name = model
        self._model = SentenceTransformer(model, device="cpu")

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(self._model.encode([text], normalize_embeddings=True)[0])


def make_embedder(model: str = JUDGMENT_EMBED_MODEL) -> Embedder:
    if model == "BAAI/bge-m3":
        try:
            return FastEmbedBgeM3(model)
        except ImportError:
            pass
    return SentenceTransformerEmbedder(model)


class JudgmentVectors:
    """把每個月的向量檔用 memory-map 開著，查詢時分塊算相似度，不會一次吃掉大量記憶體。"""

    def __init__(self, vector_dir: Path = JUDGMENT_VECTOR_DIR, embedder: Embedder | None = None):
        self.parts: list[tuple[np.ndarray, list[str]]] = []
        self.model: str | None = None
        for jf in sorted(vector_dir.glob("*.jid.json")):
            vf = jf.with_name(jf.name.removesuffix(".jid.json") + ".vec.npy")
            if not vf.exists():
                continue
            meta = json.loads(jf.read_text(encoding="utf-8"))
            if self.model and meta["model"] != self.model:
                continue  # 不同模型算的向量不能混用
            self.model = meta["model"]
            vec = np.load(vf, mmap_mode="r")
            if len(vec) == len(meta["jids"]):
                self.parts.append((vec, meta["jids"]))
        self.count = sum(len(j) for _, j in self.parts)
        self._embedder = embedder
        self._jid_pos: dict[str, tuple[int, int]] | None = None

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = make_embedder(self.model or JUDGMENT_EMBED_MODEL)
        return self._embedder

    def _top(self, q: np.ndarray, top_k: int, exclude: str | None = None) -> list[tuple[str, float]]:
        best: list[tuple[str, float]] = []
        for vec, jids in self.parts:
            for s in range(0, len(jids), CHUNK):
                sims = np.asarray(vec[s:s + CHUNK], dtype=np.float32) @ q
                k = min(top_k, len(sims))
                idx = np.argpartition(-sims, k - 1)[:k]
                best.extend((jids[s + i], float(sims[i])) for i in idx)
            best.sort(key=lambda x: -x[1])
            best = best[:top_k + 1]
        return [b for b in best if b[0] != exclude][:top_k]

    def search(self, query: str, top_k: int = 500) -> list[tuple[str, float]]:
        """回傳 [(JID, 相似度)]，相似度越接近 1 越像。"""
        if not self.count:
            return []
        return self._top(self.embedder.embed_query(query), top_k)

    def vector_of(self, jid: str) -> np.ndarray | None:
        if self._jid_pos is None:
            self._jid_pos = {j: (p, i) for p, (_, jids) in enumerate(self.parts) for i, j in enumerate(jids)}
        pos = self._jid_pos.get(jid)
        if pos is None:
            return None
        return _normalize(np.asarray(self.parts[pos[0]][0][pos[1]], dtype=np.float32))

    def similar_to(self, jid: str, top_k: int = 500) -> list[tuple[str, float]] | None:
        v = self.vector_of(jid)
        return None if v is None else self._top(v, top_k, exclude=jid)


def try_load(vector_dir: Path = JUDGMENT_VECTOR_DIR) -> JudgmentVectors | None:
    if not vector_dir.exists():
        return None
    v = JudgmentVectors(vector_dir)
    return v if v.count else None


def save_month(out_dir: Path, stem: str, jids: Sequence[str], vectors: np.ndarray, model: str) -> None:
    """Colab 筆記本用：存一個月的向量。先寫暫存檔再改名，斷線時不會留下半個檔。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    vf, jf = out_dir / f"{stem}.vec.npy", out_dir / f"{stem}.jid.json"
    tmp_v, tmp_j = out_dir / f"{stem}.vec.tmp.npy", out_dir / f"{stem}.jid.tmp"
    np.save(tmp_v, _normalize(vectors).astype(np.float16))
    tmp_j.write_text(json.dumps({"model": model, "jids": list(jids)}, ensure_ascii=False), encoding="utf-8")
    tmp_v.replace(vf)
    tmp_j.replace(jf)  # .jid.json 最後出現，代表這個月完成
