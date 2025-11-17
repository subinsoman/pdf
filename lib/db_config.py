import os
from typing import Dict, Any

try:
    import tomllib as _toml  # Python 3.11+
except Exception:  # pragma: no cover
    _toml = None

try:
    from sqlalchemy import create_engine as _sa_create_engine
except Exception:  # pragma: no cover
    _sa_create_engine = None


class DatabaseConfig:
    """Helper to load database configuration from .streamlit/config.toml.

    Supports multiple drivers using the [database] section:
      driver = "mysql" | "postgres" | "sqlite"
      host, port, user, password, name, path
    """

    def __init__(self, base_dir: str | None = None) -> None:
        # base_dir is the project root where .streamlit/config.toml lives
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(__file__))
        self.base_dir = base_dir
        self._cfg: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        cfg_path = os.path.join(self.base_dir, ".streamlit", "config.toml")
        if _toml is None or not os.path.exists(cfg_path):
            self._cfg = {}
            return
        try:
            with open(cfg_path, "rb") as f:
                data = _toml.load(f)  # type: ignore[arg-type]
        except Exception:
            data = {}
        self._cfg = data.get("database", {}) if isinstance(data, dict) else {}

    def reload(self) -> None:
        """Reload config from disk."""
        self._load()

    def as_dict(self) -> Dict[str, Any]:
        """Return the raw [database] config dict (may be empty)."""
        return dict(self._cfg) if isinstance(self._cfg, dict) else {}

    def get_driver(self) -> str:
        cfg = self.as_dict()
        driver = str(cfg.get("driver", "")).strip().lower()
        return driver or "mysql"

    def build_url(self) -> str:
        """Build a generic database URL from the config.

        - mysql   -> mysql://user:password@host:port/name
        - postgres-> postgresql://user:password@host:port/name
        - sqlite  -> sqlite:///path
        """
        cfg = self.as_dict()
        driver = self.get_driver()

        if driver == "sqlite":
            path = str(cfg.get("path", "./db.sqlite3")).strip() or "./db.sqlite3"
            if not (path.startswith("sqlite:/") or path.startswith("/")):
                # relative path from base_dir
                path = os.path.join(self.base_dir, path)
            return f"sqlite:///{path}"

        host = str(cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        port = cfg.get("port", 0) or (5432 if driver == "postgres" else 3306)
        user = str(cfg.get("user", "")).strip() or "root"
        password = str(cfg.get("password", "")).strip() or ""
        name = str(cfg.get("name", "")).strip() or "test"

        if driver == "postgres":
            scheme = "postgresql+psycopg2"
        else:
            scheme = "mysql+pymysql"

        if password:
            auth = f"{user}:{password}"
        else:
            auth = user

        return f"{scheme}://{auth}@{host}:{int(port)}/{name}"

    def create_engine(self, **kwargs):
        """Create and return a SQLAlchemy Engine for the configured database.

        Additional keyword arguments are passed directly to sqlalchemy.create_engine.
        If SQLAlchemy is not installed, this will raise a RuntimeError.
        """
        if _sa_create_engine is None:
            raise RuntimeError("SQLAlchemy is not available. Please install 'SQLAlchemy' in requirements.txt.")
        url = self.build_url()
        return _sa_create_engine(url, **kwargs)
