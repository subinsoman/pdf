import os
import json
from typing import List, Dict, Optional

class ProductStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.products_path = os.path.join(self.data_dir, "products.json")
        if not os.path.exists(self.products_path):
            with open(self.products_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _load(self) -> List[Dict]:
        try:
            with open(self.products_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self, products: List[Dict]):
        with open(self.products_path, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

    def list(self) -> List[Dict]:
        return self._load()

    def get_by_name(self, name: str) -> Optional[Dict]:
        name_lower = name.strip().lower()
        for p in self._load():
            if p.get("name", "").strip().lower() == name_lower:
                return p
        return None

    def get(self, product_id: str) -> Optional[Dict]:
        for p in self._load():
            if p.get("id") == product_id:
                return p
        return None

    def upsert(self, product: Dict):
        products = self._load()
        found = False
        for i, p in enumerate(products):
            if p.get("id") == product.get("id"):
                products[i] = product
                found = True
                break
        if not found:
            products.append(product)
        self._save(products)

    def delete(self, product_id: str):
        products = self._load()
        products = [p for p in products if p.get("id") != product_id]
        self._save(products)
