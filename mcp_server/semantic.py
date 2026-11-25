"""語意搜尋：把條文轉成向量（embedding），查詢時找「意思最接近」的條文。

- 模型：預設 BAAI/bge-small-zh-v1.5（約 100MB，CPU 就能跑），透過 fastembed（ONNX，不用裝 PyTorch）。
- 向量檔：build_embeddings.py 產生 mcp_server/vectors/<模型>.npz，內含條文 id 與正規化後的向量。
- 沒有向量檔或沒裝 fastembed 時，搜尋會自動退回只用關鍵字，不會壞掉。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

HERE = Path(__file__).resolve().parent
VECTOR_DIR = Path(os.environ.get("LAW_VECTOR_DIR", HERE / "vectors"))
DEFAULT_MODEL = os.environ.get("LAW_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

# bge 中文模型建議查詢時加上這段指令（條文本身不用加）
QUERY_PREFIX = {
    "BAAI/bge-small-zh-v1.5": "为这个句子生成表示以用于检索相关文章：",
}


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-12)


class FastEmbedder:
    """用 fastembed 載入模型。第一次使用會自動下載模型（約 100MB）。"""

    def __init__(self, model: str = DEFAULT_MODEL, threads: int | None = None):
        from fastembed import TextEmbedding  # 延後匯入：沒裝也不影響關鍵字搜尋

        self.name = model
        self._model = TextEmbedding(model_name=model, threads=threads)
        self._prefix = QUERY_PREFIX.get(model, "")

    def embed_documents(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        return _normalize(np.array(list(self._model.embed(list(texts), batch_size=batch_size))))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(np.array(list(self._model.embed([self._prefix + text])))[0])


def slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)


def vector_path(model: str = DEFAULT_MODEL, vector_dir: Path = VECTOR_DIR) -> Path:
    return vector_dir / f"{slug(model)}.npz"


def article_passage(law_name: str, chapter: str, art_display: str, text: str, max_chars: int = 400) -> str:
    """送進模型的條文文字：加上法規名稱與章名，讓模型知道上下文。"""
    head = f"{law_name} {chapter} {art_display}".strip()
    return f"{head}：{text.replace(chr(13), '').replace(chr(10), ' ')}"[:max_chars]


class VectorIndex:
    """記憶體中的向量索引。4 萬條 × 512 維約 40MB，用矩陣乘法暴力比對就很快。"""

    def __init__(self, ids: np.ndarray, vectors: np.ndarray, model: str, embedder: Embedder | None = None):
        self.ids = ids.astype(np.int64)
        self.vectors = _normalize(vectors.astype(np.float32))
        self.model = model
        self._embedder = embedder

    @classmethod
    def load(cls, path: Path, embedder: Embedder | None = None) -> "VectorIndex":
        data = np.load(path, allow_pickle=False)
        meta = json.loads(str(data["meta"]))
        return cls(data["ids"], data["vectors"], meta["model"], embedder)

    def save(self, path: Path, extra_meta: dict | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"model": self.model, "count": int(len(self.ids)), **(extra_meta or {})}
        np.savez(path, ids=self.ids, vectors=self.vectors.astype(np.float16),
                 meta=np.array(json.dumps(meta, ensure_ascii=False)))

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = FastEmbedder(self.model)
        return self._embedder

    def search(self, query: str, top_k: int = 300) -> list[tuple[int, float]]:
        """回傳 [(條文 id, 相似度)]，相似度越接近 1 越相關。"""
        q = self.embedder.embed_query(query)
        sims = self.vectors @ q
        k = min(top_k, len(sims))
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(self.ids[i]), float(sims[i])) for i in idx]


def try_load(model: str = DEFAULT_MODEL, vector_dir: Path = VECTOR_DIR) -> VectorIndex | None:
    path = vector_path(model, vector_dir)
    if not path.exists():
        return None
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return None
    return VectorIndex.load(path)
