import os
import json
from typing import List, Dict

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class Retriever:
    def __init__(self, text_dir: str):
        self.text_dir = text_dir
        os.makedirs(self.text_dir, exist_ok=True)
        # in-memory cache: product_id -> {"chunks": List[str], "vectorizer": TfidfVectorizer, "matrix": csr_matrix}
        self._cache: Dict[str, Dict] = {}

    def _chunks_path(self, product_id: str) -> str:
        return os.path.join(self.text_dir, f"{product_id}.json")

    def index_product(self, product_id: str, chunks: List[str]):
        # Persist chunks
        with open(self._chunks_path(product_id), "w", encoding="utf-8") as f:
            json.dump(chunks, f, ensure_ascii=False, indent=2)
        # Build index in memory
        self._build_index(product_id, chunks)

    def _load_chunks(self, product_id: str) -> List[str]:
        path = self._chunks_path(product_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _build_index(self, product_id: str, chunks: List[str]):
        vectorizer = TfidfVectorizer(stop_words="english")
        if not chunks:
            # Avoid fitting empty
            chunks = [""]
        matrix = vectorizer.fit_transform(chunks)
        self._cache[product_id] = {
            "chunks": chunks,
            "vectorizer": vectorizer,
            "matrix": matrix,
        }

    def _ensure_index(self, product_id: str):
        if product_id in self._cache:
            return
        chunks = self._load_chunks(product_id)
        self._build_index(product_id, chunks)

    def query(self, product_id: str, question: str, top_k: int = 3) -> List[Dict]:
        self._ensure_index(product_id)
        entry = self._cache.get(product_id)
        if not entry:
            return []
        vec = entry["vectorizer"].transform([question])
        sims = cosine_similarity(vec, entry["matrix"]).flatten()
        # Get top_k indices
        idxs = sims.argsort()[::-1][:max(top_k, 1)]
        results: List[Dict] = []
        for i in idxs:
            results.append({
                "text": entry["chunks"][i],
                "score": float(sims[i]),
            })
        return results
