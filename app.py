import os
import warnings
# Suppress st.cache deprecation warnings from third-party libraries
warnings.filterwarnings("ignore", message=".*st\\.cache.*is deprecated.*")
import requests
import urllib.parse
import io
import base64
import ssl
import time
import html
try:
    import tomllib as _toml  # Python 3.11+
except Exception:  # pragma: no cover
    _toml = None
import uuid
import json
from datetime import datetime
import streamlit as st
from streamlit_option_menu import option_menu
import pandas as pd
import sys
import types
# Compatibility shim: some third-party libs import json_normalize from pandas.io.json,
# which was removed in pandas>=1.0. Provide a module alias to the top-level function.
try:  # pragma: no cover
    import pandas.io.json as _pd_json  # type: ignore
    _ = getattr(_pd_json, "json_normalize")
except Exception:  # pragma: no cover
    try:
        from pandas import json_normalize as _json_normalize
        # Ensure parent package alias exists in sys.modules
        sys.modules.setdefault("pandas.io", types.ModuleType("pandas.io"))
        json_mod = types.ModuleType("pandas.io.json")
        setattr(json_mod, "json_normalize", _json_normalize)
        sys.modules["pandas.io.json"] = json_mod
    except Exception:
        pass
from awesome_table import AwesomeTable
from streamlit_extras.colored_header import colored_header
from typing import List, Dict, Optional
try:
    from websocket import create_connection  # type: ignore
except Exception:
    create_connection = None  # type: ignore
import threading
import queue
try:
    from streamlit_cookies_manager import EncryptedCookieManager  # type: ignore
except Exception:  # pragma: no cover
    EncryptedCookieManager = None  # type: ignore

from lib.storage import ProductStore
from lib.db_config import DatabaseConfig
from lib.pdf_utils import extract_text_from_pdf, chunk_text
from lib.retriever import Retriever
from lib.pdf_metadata_repo import PdfMetadataRepository
from dashboard import Dashboard
# OAuth component will be lazy-imported inside the auth function to avoid NameError interruptions

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")
TEXT_DIR = os.path.join(DATA_DIR, "texts")

os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(TEXT_DIR, exist_ok=True)

# Load optional custom config from .streamlit/config.toml
CONFIG_TOML: Dict[str, Dict] = {}
try:
    cfg_path = os.path.join(os.path.dirname(__file__), ".streamlit", "config.toml")
    if _toml is not None and os.path.exists(cfg_path):
        with open(cfg_path, "rb") as _cf:
            CONFIG_TOML = _toml.load(_cf)  # type: ignore
except Exception:
    CONFIG_TOML = {}

# ---------------------- Config + DB Helpers ----------------------
DB_CONFIG = DatabaseConfig(base_dir=os.path.dirname(__file__))

def get_database_config() -> Dict:
    """Return database configuration dict loaded from .streamlit/config.toml.

    This uses DatabaseConfig helper so callers can access driver/host/port/user/etc.
    """
    return DB_CONFIG.as_dict()

def get_database_url() -> str:
    """Return a generic database URL based on [database] in config.toml."""
    return DB_CONFIG.build_url()

def get_database_engine(**kwargs):
    """Return a SQLAlchemy Engine built from the configured database URL.

    Extra keyword arguments are passed directly to sqlalchemy.create_engine.
    """
    return DB_CONFIG.create_engine(**kwargs)

def get_pdf_metadata_repo() -> PdfMetadataRepository:
    """Return a PdfMetadataRepository bound to the configured database engine.

    This is read-only in the sense that callers should only use list/get
    if they want to avoid mutating the database.
    """
    engine = get_database_engine()
    return PdfMetadataRepository(engine)

STATUS_LABELS = {
    0: "New",
    1: "Success",
    2: "In Progress",
    3: "Deleted",
}

def _enrich_rows_with_status_label(rows: List[Dict]) -> List[Dict]:
    """Add a 'status_label' field to each row based on status code.
    
    Also filters out deleted rows (status=3) and converts datetime objects to strings.
    """
    filtered = []
    for r in rows:
        try:
            code = int(r.get("status", 0))
        except Exception:
            code = 0
        # Skip deleted entries
        if code == 3:
            continue
        r["status_label"] = STATUS_LABELS.get(code, "Unknown")
        
        # Convert datetime objects to ISO strings for JSON compatibility
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = v.isoformat()
        
        filtered.append(r)
    return filtered

def _get_ws_url() -> str:
    try:
        url = (str(
            (CONFIG_TOML.get("custom", {}) or {}).get("AARYA_WEBSOCKET_URL")
            or CONFIG_TOML.get("AARYA_WEBSOCKET_URL")
            or ((st.secrets.get("aarya_ws") if hasattr(st, "secrets") else None))
            or os.environ.get("AARYA_WEBSOCKET_URL")
            or ""
        )).strip()
        
        # Append client_id, client_name, and user_email as query parameters
        if url:
            # Get user session id (client_id)
            client_id = st.session_state.get("aarya_session_id", "")
            if not client_id:
                client_id = f"session_{uuid.uuid4().hex[:8]}_{int(time.time()*1000)}"
                st.session_state["aarya_session_id"] = client_id
            
            # Get logged in user information
            user = st.session_state.get("user") or {}
            client_name = user.get("name", "")
            user_email = user.get("email", "")
            
            # Parse URL and append query parameters
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            
            # Add/update parameters
            params["client_id"] = [client_id]
            if client_name:
                params["client_name"] = [client_name]
            if user_email:
                params["user_email"] = [user_email]
            
            # Reconstruct URL with updated query parameters
            new_query = urllib.parse.urlencode(params, doseq=True)
            url = urllib.parse.urlunparse((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment
            ))
    except Exception:
        url = ""
    return url

def _get_workflow_id() -> str:
    try:
        wid = (str(
            (CONFIG_TOML.get("custom", {}) or {}).get("WORKFLOW_ID")
            or CONFIG_TOML.get("WORKFLOW_ID")
            or ((st.secrets.get("workflow_id") if hasattr(st, "secrets") else None))
            or os.environ.get("WORKFLOW_ID")
            or ""
        )).strip()
        
        # If not found, try to extract from WebSocket URL query parameters
        if not wid:
            ws_url = _get_ws_url()
            if ws_url and "workflow_id=" in ws_url:
                try:
                    parsed = urllib.parse.urlparse(ws_url)
                    params = urllib.parse.parse_qs(parsed.query)
                    if "workflow_id" in params:
                        wid = params["workflow_id"][0]
                except Exception:
                    pass
    except Exception:
        wid = ""
    return wid

def _ws_send_message(ws_url: str, payload: Dict, debug: bool = False) -> Optional[str]:
    if not ws_url or create_connection is None:
        st.error("❌ WebSocket library not available. Please install: pip install websocket-client")
        return None
    try:
        opts = {}
        try:
            if ws_url.lower().startswith("wss://"):
                opts = {"sslopt": {"cert_reqs": ssl.CERT_NONE}}  # ignore cert for internal/self-signed
        except Exception:
            opts = {}
        
        if debug:
            st.info(f"🔌 Connecting to WebSocket: {ws_url}")
        
        # No timeout - keep connection open indefinitely until response received
        ws = create_connection(ws_url, timeout=None, **opts)
        
        # Set socket to never timeout while waiting for response
        ws.sock.settimeout(None)
        
        # Send message
        payload_str = json.dumps(payload)
        if debug:
            st.info(f"📤 Sending payload: {payload_str[:200]}...")
        ws.send(payload_str)
        
        # Receive response - will wait indefinitely until message arrives
        if debug:
            st.info("⏳ Waiting for response (no timeout)...")
        resp = ws.recv()
        if debug:
            st.success("✅ Received response from WebSocket")
        
        try:
            ws.close()
        except Exception:
            pass
        
        # Decode bytes to string if needed
        resp_str = None
        if isinstance(resp, (bytes, bytearray)):
            try:
                resp_str = resp.decode("utf-8", errors="ignore")
            except Exception as e:
                st.error(f"❌ Failed to decode response: {e}")
                return None
        else:
            resp_str = str(resp)
        
        # Parse JSON response and extract output
        try:
            resp_json = json.loads(resp_str)
            
            # Extract output from the response structure
            if isinstance(resp_json, dict):
                # Try direct output field first (top-level)
                output = resp_json.get("output")
                
                # If not found, try message.output (nested)
                if not output:
                    message = resp_json.get("message", {})
                    if isinstance(message, dict):
                        output = message.get("output")
                
                if output:
                    if debug:
                        with st.expander("🔍 Full WebSocket Response (Debug)", expanded=False):
                            st.json(resp_json)
                        st.info(f"📝 Extracted output: {output[:100]}...")
                    return output
            
            # Fallback to raw response if structure doesn't match
            if debug:
                st.warning("⚠️ Response doesn't match expected format, returning raw response")
                with st.expander("🔍 Raw Response (Debug)", expanded=False):
                    st.code(resp_str)
            return resp_str
        except json.JSONDecodeError:
            # If not JSON, return as-is
            if debug:
                st.warning("⚠️ Response is not JSON, returning raw text")
                with st.expander("🔍 Raw Response (Debug)", expanded=False):
                    st.code(resp_str)
            return resp_str
    except Exception as e:
        st.error(f"❌ WebSocket error: {type(e).__name__}: {str(e)}")
        import traceback
        if debug:
            st.code(traceback.format_exc())
        return None

# ---------------------- Auth Helpers ----------------------
def get_admin_password() -> str:
    # Prefer secrets, fallback to environment variable, then default
    val = None
    try:
        val = st.secrets.get("ADMIN_PASSWORD") if hasattr(st, "secrets") else None
    except Exception:
        val = None
    if not val:
        val = os.getenv("ADMIN_PASSWORD")
    return str(val) if val else "admin"


def _get_admin_emails() -> List[str]:
    emails: List[str] = []
    # 1) Environment variable (comma-separated)
    env_val = os.getenv("ADMIN_EMAILS", "").strip()
    if env_val:
        emails.extend([e.strip().lower() for e in env_val.split(",") if e.strip()])
    # 2) Secrets (list or comma-separated string)
    try:
        sec_val = st.secrets.get("ADMIN_USERS", "") if hasattr(st, "secrets") else ""
        if isinstance(sec_val, list):
            emails.extend([str(e).strip().lower() for e in sec_val if str(e).strip()])
        elif isinstance(sec_val, str) and sec_val.strip():
            emails.extend([e.strip().lower() for e in sec_val.split(",") if e.strip()])
    except Exception:
        pass
    # 3) .streamlit/config.toml under [app].admin_users
    try:
        cfg_path = os.path.join(os.path.dirname(__file__), ".streamlit", "config.toml")
        if os.path.exists(cfg_path) and _toml is not None:
            with open(cfg_path, "rb") as f:
                data = _toml.load(f)
            app_cfg = data.get("app", {}) if isinstance(data, dict) else {}
            cfg_emails = app_cfg.get("admin_users", [])
            if isinstance(cfg_emails, list):
                emails.extend([str(e).strip().lower() for e in cfg_emails if str(e).strip()])
    except Exception:
        pass
    # De-duplicate
    out = []
    seen = set()
    for e in emails:
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def is_admin_authenticated() -> bool:
    # Password-based gate via session flag only
    return bool(st.session_state.get("is_admin", False))


def is_admin_user() -> bool:
    """Return True if the logged-in user's email is present in admin list."""
    user = st.session_state.get("user") or {}
    email = (user.get("email") or "").strip().lower()
    return bool(email) and (email in _get_admin_emails())


def admin_login_form():
    with st.form("admin_login"):
        pwd = st.text_input("Admin password", type="password")
        submit = st.form_submit_button("Login")
    if submit:
        if pwd == get_admin_password():
            st.session_state["is_admin"] = True
            st.success("Logged in as admin")
        else:
            st.error("Invalid password")


# ---------------------- App Init ----------------------
if "store" not in st.session_state:
    st.session_state["store"] = ProductStore(DATA_DIR)
if "retriever" not in st.session_state:
    st.session_state["retriever"] = Retriever(TEXT_DIR)
if "chat_histories" not in st.session_state:
    st.session_state["chat_histories"] = {}
if "show_create_dialog" not in st.session_state:
    st.session_state["show_create_dialog"] = False
if "nav_page" not in st.session_state:
    st.session_state["nav_page"] = "Dashbord"
if "user" not in st.session_state:
    st.session_state["user"] = None
if "show_profile" not in st.session_state:
    st.session_state["show_profile"] = False
if "google_access_token" not in st.session_state:
    st.session_state["google_access_token"] = None

# Initialize cookies (used to persist login across refresh)
cookies = None
try:
    if EncryptedCookieManager is not None:
        cookies = EncryptedCookieManager(prefix="sixdee_app", password=st.secrets.get("COOKIE_PASSWORD", "dev-secret"))
        if not cookies.ready():
            st.stop()
except Exception:
    cookies = None

# Handle logout via query param so we can place a Logout link in the toolbar overlay
def _read_query_params() -> dict:
    """Return a normalized dict[str, list[str]] of query params for all Streamlit versions."""
    try:
        qp = st.query_params  # Streamlit >= 1.30
    except Exception:
        qp = st.experimental_get_query_params()  # older
    out = {}
    try:
        # Handle both dict-like and QueryParamsProxy objects
        if hasattr(qp, 'to_dict'):
            # QueryParamsProxy has to_dict() method
            qp_dict = qp.to_dict()
        else:
            qp_dict = dict(qp.items())
        
        for k, v in qp_dict.items():
            if isinstance(v, list):
                out[k] = [str(x) for x in v]
            elif v is None:
                out[k] = []
            else:
                out[k] = [str(v)]
    except Exception:
        pass
    return out

def _clear_query_params():
    try:
        st.query_params.clear()
    except Exception:
        try:
            st.experimental_set_query_params()
        except Exception:
            pass

def _logout():
    # Best-effort revoke Google access token
    try:
        token = st.session_state.get("google_access_token")
        if token:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": token},
                timeout=5,
            )
    except Exception:
        pass
    # Clear user cookie FIRST before clearing session
    try:
        if cookies is not None and cookies.ready():
            # Try multiple approaches to ensure cookie is cleared
            try:
                if "user" in cookies:
                    del cookies["user"]
            except Exception:
                pass
            try:
                cookies["user"] = ""
            except Exception:
                pass
            try:
                cookies.save()
            except Exception:
                pass
    except Exception:
        pass
    # Clear all session state related to authentication
    keys_to_clear = ["user", "google_access_token", "show_profile", "is_admin"]
    for key in keys_to_clear:
        if key in st.session_state:
            st.session_state[key] = None if key == "user" or key == "google_access_token" else False
    # Set a flash query param so we can show an alert after rerun on the login screen
    try:
        st.query_params.clear()
        st.query_params["logged_out"] = "1"
    except Exception:
        try:
            st.experimental_set_query_params(logged_out="1")
        except Exception:
            pass
    st.rerun()

def _handle_logout_param():
    params = _read_query_params()
    has_logout = "logout" in params and ("1" in params.get("logout", []))
    if has_logout:
        _logout()

def _handle_kb_action():
    params = _read_query_params()
    action = None
    pid = None
    try:
        action = (params.get("kb_action", []) or [None])[0]
        pid = (params.get("pid", []) or [None])[0]
    except Exception:
        action = None
        pid = None
    if action and pid:
        if action == "edit":
            st.session_state["kb_selected_rows"] = [{"id": pid}]
            _clear_query_params()
            st.rerun()
        elif action == "delete":
            cur = None
            try:
                cur = store.get(pid)
            except Exception:
                cur = None
            try:
                upload_url = (str(
                    (CONFIG_TOML.get("custom", {}) or {}).get("UPLOAD_WEBHOOK_URL")
                    or CONFIG_TOML.get("UPLOAD_WEBHOOK_URL")
                    or ((st.secrets.get("configurl") if hasattr(st, "secrets") else None))
                    or st.secrets.get("upload_url")
                    or os.environ.get("CONFIG_URL")
                    or os.environ.get("UPLOAD_WEBHOOK_URL")
                    or ""
                )).strip()
            except Exception:
                upload_url = ""
            if not upload_url:
                st.warning("Upload webhook URL not configured; delete webhook not sent.")
            if upload_url and cur:
                try:
                    user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                    pdfp = cur.get("pdf_path", "") or os.path.join(PDF_DIR, f"{pid}.pdf")
                    _payload = {
                        "id": pid,
                        "name": cur.get("name", ""),
                        "operation": "delete",
                        "description": cur.get("description", ""),
                        "pdf_path": pdfp,
                        "created_by": cur.get("created_by", ""),
                        "created_at": cur.get("created_at", ""),
                        "updated_by": user_email,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    st.info(f"Calling delete webhook: {upload_url}")
                    _resp = requests.post(
                        upload_url,
                        json=_payload,
                        timeout=20,
                    )
                    try:
                        _j = _resp.json()
                        if bool(_j.get("success")):
                            st.success(str(_j.get("message") or "Delete webhook acknowledged"))
                        else:
                            st.warning(str(_j.get("message") or "Delete webhook did not confirm success"))
                    except Exception:
                        st.warning(f"Delete webhook responded without JSON (status {getattr(_resp,'status_code',None)}): {getattr(_resp,'text', '')[:200]}")
                except Exception:
                    st.warning("Failed to call delete webhook")
            # Removed: store.delete(pid) - using database only
            try:
                pdfp = os.path.join(PDF_DIR, f"{pid}.pdf")
                if os.path.exists(pdfp): os.remove(pdfp)
            except Exception:
                pass
            try:
                chunkp = os.path.join(TEXT_DIR, f"{pid}.json")
                if os.path.exists(chunkp): os.remove(chunkp)
            except Exception:
                pass
            st.success("Deleted 1 row.")
            _clear_query_params()
            st.rerun()

_handle_logout_param()
_handle_kb_action()

# Restore user from cookie if session empty, but do NOT restore right after logout
if not st.session_state.get("user") and cookies is not None and cookies.ready():
    _qp_restore = {}
    try:
        _qp_restore = _read_query_params()
    except Exception:
        _qp_restore = {}
    # Don't restore if logged_out or logout param is present
    if "logged_out" not in _qp_restore and "logout" not in _qp_restore:
        try:
            raw = cookies.get("user")
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("email"):
                    st.session_state["user"] = {
                        "email": data.get("email"),
                        "name": data.get("name") or data.get("email"),
                        "picture": data.get("picture"),
                        "sub": data.get("sub"),
                    }
        except Exception:
            pass

# Profile dropdown visibility is controlled by session flag and a transparent button overlay on the chip

store: ProductStore = st.session_state["store"]
retriever: Retriever = st.session_state["retriever"]

# Asset paths
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
ICON_PATH = os.path.join(ASSETS_DIR, "6D_fav_icon.ico")
LOGO_SVG_PATH = os.path.join(ASSETS_DIR, "logo.svg")

# Page config with custom favicon if available
page_icon = ICON_PATH if os.path.exists(ICON_PATH) else "📄"
st.set_page_config(page_title="Knowledgebase Dashboard", page_icon=page_icon, layout="wide", initial_sidebar_state="expanded")

# Prefer native logo placement (goes into stLogoSpacer) — use logo.svg for navbar
LOGO1_SVG_PATH = os.path.join(ASSETS_DIR, "logo1.svg")
_chosen_logo_rel = "assets/logo.svg" if os.path.exists(LOGO_SVG_PATH) else ("assets/logo1.svg" if os.path.exists(LOGO1_SVG_PATH) else None)
try:
    if st.session_state.get("user") and _chosen_logo_rel:
        st.logo(_chosen_logo_rel)
except Exception:
    pass

# Simple header title (branding moved to sidebar) and global top title bar
# Navbar logo uses st.logo (set above). Toolbar icon uses logo1 if present; fallback to logo.svg.
_nav_logo_rel = _chosen_logo_rel
_logo1_rel = "assets/logo1.svg" if os.path.exists(LOGO1_SVG_PATH) else _nav_logo_rel

# (Removed separate in-body top title bar; title will be rendered in the toolbar itself)

# Global CSS for a more professional look
st.markdown(
    """
    <style>
      :root {
        --accent: #2563eb;
        --bg-card: #ffffff;
        --border: #e5e7eb;
        --sidebar-bg: #f3f5f9;
        --sidebar-text: #1f2937;
      }
      html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {
        font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", sans-serif;
      }
      /* Tweak padding: keep left/right as default; set only top to 1px */
      .st-emotion-cache-liupi { padding-top: 1px !important; }
      /* Main block container (stable selector) */
      div[data-testid='stMainBlockContainer'] { padding-top: 1px !important; }
      /* Fallback for older versions */
      section.main > div.block-container { padding-top: 1px !important; }
      /* Hide Deploy button in toolbar */
      div[data-testid='stAppDeployButton'] { display: none !important; }
      /* Hide Streamlit MainMenu (cover multiple versions/selectors) */
      div[data-testid="stMainMenu"], #MainMenu { display: none !important; visibility: hidden !important; }
      /* Put title into Streamlit's top toolbar and allow right-side chip */
      div[data-testid="stToolbar"] { position: relative; overflow: visible; padding-right: 180px; }
      /* Prevent Streamlit toolbar actions from intercepting clicks over our chip */
      div[data-testid="stToolbarActions"] { pointer-events: none !important; }
      .tb-chip, .tb-profile, .tb-profile-menu { pointer-events: auto; }
      /* Native menu styles */
      details.tb-profile-menu { position: fixed; top: 8px; right: 16px; z-index: 2147483647; }
      details.tb-profile-menu > summary { list-style: none; display: inline-block; cursor: pointer; }
      details.tb-profile-menu > summary::-webkit-details-marker { display: none; }
      details.tb-profile-menu .chip { display:flex; align-items:center; gap:8px; background:#fff; border:1px solid #e5e7eb; border-radius:9999px; padding:4px 10px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); cursor: pointer; }
      details.tb-profile-menu[open] .chip { box-shadow: 0 6px 18px rgba(0,0,0,0.12); }
      details.tb-profile-menu .avatar { width:24px; height:24px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:13px; background:#e5e7eb; color:#374151; overflow:hidden; }
      details.tb-profile-menu .avatar img { width:100%; height:100%; object-fit: cover; display:block; }
      details.tb-profile-menu .name span { font-size:13px; color:#111827; max-width: 180px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display:block; }
      details.tb-profile-menu .menu { position: fixed; top: 44px; right: 16px; z-index: 2147483651; background:#fff; border:1px solid #e5e7eb; border-radius:12px; box-shadow:0 8px 24px rgba(0,0,0,0.12); min-width: 260px; max-width: 320px; overflow:hidden; }
      details.tb-profile-menu .menu .row { display:flex; align-items:center; gap:12px; padding: 12px 14px; }
      details.tb-profile-menu .menu .row + .row { border-top: 1px solid #f1f5f9; }
      details.tb-profile-menu .menu .avatar-xl { width:40px; height:40px; border-radius:50%; background:#e5e7eb; color:#374151; display:flex; align-items:center; justify-content:center; font-size:16px; overflow:hidden; }
      details.tb-profile-menu .menu .name { font-weight:600; color:#111827; }
      details.tb-profile-menu .menu .email { font-size:12px; color:#6b7280; }
      details.tb-profile-menu .menu .logout-btn { margin-left:auto; color:#ef4444; text-decoration:none; font-size:13px; }
      details.tb-profile-menu .menu .logout-btn:hover { text-decoration: underline; }
      /* Toolbar left icon (logo1) */
      div[data-testid="stToolbar"]::before {
        content: "";
        position: absolute;
        left: 16px;
        top: 50%;
        transform: translateY(-50%);
        width: 18px;
        height: 18px;
        background-image: url('assets/logo1.svg');
        background-repeat: no-repeat;
        background-size: contain;
      }
      div[data-testid="stToolbar"]::after {
        content: "Knowledgebase Dashboard";
        position: absolute;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -50%);
        font-weight: 600;
        font-size: 14px;
        color: #111827;
        letter-spacing: 0.2px;
        pointer-events: none;
        max-width: calc(100% - 500px); /* avoid overlap with right-side toolbar icons */
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      /* When sidebar is collapsed, title stays centered */
      [data-testid="stSidebar"][aria-expanded="false"] ~ [data-testid="stAppViewContainer"] div[data-testid="stToolbar"]::after {
        left: 50%;
        transform: translate(-50%, -50%);
      }
      .app-card {
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 16px 18px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.03);
        margin-bottom: 16px;
      }
      .app-section-title {
        margin: 0 0 12px 0;
        font-size: 1.1rem;
        font-weight: 600;
      }
      .app-muted { color: #6b7280; }
      .stButton>button[kind="primary"], .stButton>button {
        border-radius: 8px !important;
        padding: 0.5rem 0.9rem !important;
        width: auto !important;
        max-width: none !important;
        white-space: nowrap !important;
        display: inline-flex !important;
        align-items: center !important;
      }
      /* Remove extra outer spacing around action buttons (Create/Clear/Save/Delete) */
      div.element-container.st-key-kb_ce_new_clear,
      div.element-container.st-key-kb_ce_new_submit,
      div.element-container[class*="st-key-kb_ce_save_"],
      div.element-container[class*="st-key-kb_ce_clear_"],
      div.element-container[class*="st-key-kb_delete_pdf_"] {
        margin: 0 !important;
        padding: 0 !important;
      }
      /* Tighten the action row columns containing these buttons */
      /* Applies where our two/three-column action bars are rendered */
      [data-testid="stHorizontalBlock"] .stButton { margin: 0 !important; }
      [data-testid="stHorizontalBlock"] { gap: 0 !important; }
      /* Ensure columns that HOLD our action buttons do not get forced widths */
      [data-testid="stColumn"]:has(.st-key-kb_ce_new_clear),
      [data-testid="stColumn"]:has(.st-key-kb_ce_new_submit),
      [data-testid="stColumn"]:has([class*="st-key-kb_ce_save_"]),
      [data-testid="stColumn"]:has([class*="st-key-kb_ce_clear_"]),
      [data-testid="stColumn"]:has([class*="st-key-kb_delete_pdf_"]) {
        width: auto !important;
        flex: 0 0 auto !important;
      }
      /* Hide Streamlit spinners and running indicators */
      div[data-testid="stSpinner"],
      .stSpinner,
      div[aria-live="polite"][data-baseweb="notification"] {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
      }
      /* Sidebar radio spacing */
      section[data-testid="stSidebar"] label { margin-bottom: 4px; }

      /* Sidebar look & option-menu polish */
      section[data-testid="stSidebar"] {
        background: var(--sidebar-bg);
        background-image: none !important; /* remove theme gradients */
      }
      /* Make option_menu UL fully transparent to inherit sidebar background */
      section[data-testid="stSidebar"] ul.nav,
      section[data-testid="stSidebar"] ul.nav.nav-pills,
      section[data-testid="stSidebar"] ul.nav.nav-pills * {
        background: transparent !important;
        box-shadow: none !important;
        border: 0 !important;
      }
      section[data-testid="stSidebar"] ul.nav { margin: 0 !important; padding: 0 !important; border-radius: 0 !important; }
      section[data-testid="stSidebar"] ul.nav > li { margin: 0 !important; }
      /* Keep selected item highlight while base stays unified */
      section[data-testid="stSidebar"] ul.nav > li > a.nav-link.active {
        background-color: #eaf2ff !important;
        color: #111827 !important;
      }
      /* Remove inner padding so menu spans full width */
      section[data-testid="stSidebar"] .block-container { padding: 0 !important; }
      section[data-testid="stSidebar"] .block-container > div { margin: 0 !important; padding: 0 !important; background: var(--sidebar-bg) !important; }
      /* Sidebar width */
      div[data-testid="stSidebar"] {
        min-width: 260px; /* fallback */
        width: 260px;
      }
      .nav-brand { position: sticky; top: 0; z-index: 10; margin: -14px 0 0 0; padding: 0 4px 4px 4px; display: none; }
      .brand-logo { display: flex; align-items: center; line-height: 0; }
      .brand-logo svg { height: 20px; width: auto; display: block; margin: 0; padding: 0; transform: translateY(4px); }
      .nav-logo { filter: none; }
      .nav-divider { height: 1px; background: var(--border); margin: 8px 0 12px 0; }
      .nav-clean ul.nav { gap: 2px; padding-left: 0 !important; }
      .nav-clean a.nav-link {
        color: var(--sidebar-text) !important;
        border-radius: 10px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        transition: background 120ms ease, color 120ms ease;
      }
      .nav-clean a.nav-link:hover { background: #eef3ff !important; }
      .nav-clean a.nav-link.active {
        background: #eaf2ff !important;
        font-weight: 600 !important;
        box-shadow: inset 3px 0 0 0 var(--accent);
        color: #0f172a !important;
      }
      .nav-clean i { font-size: 16px !important; }
      /* Inline brand fallback (guaranteed render) */
      .nav-inline-brand { display:flex; align-items:center; height:44px; padding: 0 8px 0 10px; margin: 0 0 6px 0; }
      .nav-inline-brand svg { height:22px; width:auto; display:block; }
      /* Chat UI polish */
      .chat-ts { text-align: right; color: #6b7280; font-size: 12px; }
      .chip { display:none; }
      .stButton { margin: 0 !important; }
      .bubble-user { background:#ffffff; border:1px solid #e5e7eb; border-radius: 12px; padding:10px 12px; margin-bottom:6px; }
      .bubble-assistant { background:#ffffff; border:1px solid #dbe4ff; border-radius: 12px; padding:10px 12px; margin-bottom:6px; }
      .assistant-msg-wrapper { background:#ffffff; border:1px solid #dbe4ff; border-radius: 12px; padding:10px 12px; margin-bottom:6px; }
      .assistant-msg-wrapper p:first-child { margin-top: 0; }
      .assistant-msg-wrapper p:last-child { margin-bottom: 0; }
      .assistant-msg-wrapper ul, .assistant-msg-wrapper ol { margin: 0.5em 0; }
      .assistant-msg-wrapper code { background: #e0e7ff; padding: 2px 6px; border-radius: 4px; }
      .assistant-msg-wrapper pre { background: #e0e7ff; padding: 8px; border-radius: 6px; overflow-x: auto; }
      .meta-row { font-size:12px; color:#6b7280; margin-top:2px; }
      .msg-header { display:flex; align-items:center; gap:8px; font-weight:600; margin: 2px 0 6px 0; }
      .msg-header.right { justify-content: flex-end; }
      .avatar { width:24px; height:24px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:14px; }
      .avatar-user { background:#e5e7eb; color:#374151; }
      .avatar-assistant { background:#dbe4ff; color:#1d4ed8; }
      .name { font-size:13px; color:#111827; }
      /* Reduce spacing between components in Aarya page */
      .element-container { margin-bottom: 0.25rem !important; }
      div[data-testid="column"] { gap: 0.25rem !important; }
      div[data-testid="stVerticalBlock"] > div { gap: 0.25rem !important; }
      div[data-testid="stVerticalBlock"] { gap: 0.25rem !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Ensure toolbar icon uses logo1 (fallbacks to navbar logo) without touching the large CSS block
if _logo1_rel:
    st.markdown(
        f"""
        <style>
          div[data-testid='stToolbar']::before {{
            background-image: url('{_logo1_rel}') !important;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Ensure Google OAuth button style matches Sign In button (compact)
    st.markdown(
        """
        <style>
          /* Force OAuth button to match Sign In size/shape */
          div.st-key-google button {
            font-size: 10px !important;
            font-weight: 400 !important; /* normal, not bold */
            text-transform: none !important;
            padding: 12px 14px !important; /* match Sign In button */
            min-height: 44px !important;   /* match Sign In button */
            border-radius: 8px !important; /* match Sign In button */
            width: 100% !important;
            max-width: 320px !important;
            margin: 8px auto 0 !important;
            box-sizing: border-box !important;
          }
          /* Remove the big left badge/icon that makes it look wider */
          div.st-key-google button::before,
          div.st-key-google button::after {
            content: none !important;
            background: none !important;
            width: 0 !important;
            height: 0 !important;
            display: none !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

# (Removed external icon dependency for reactions; no like/dislike buttons shown)

# ----- Google OAuth helpers (placed before first use) -----
def _get_google_cfg():
    cid = st.secrets.get("google", {}).get("client_id") if hasattr(st, "secrets") else None
    csec = st.secrets.get("google", {}).get("client_secret") if hasattr(st, "secrets") else None
    redir = st.secrets.get("google", {}).get("redirect_uri") if hasattr(st, "secrets") else None
    cid = os.getenv("GOOGLE_CLIENT_ID", cid)
    csec = os.getenv("GOOGLE_CLIENT_SECRET", csec)
    redir = os.getenv("GOOGLE_REDIRECT_URI", redir)
    return cid, csec, redir


def _render_auth():
    cid, csec, redir = _get_google_cfg()
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    revoke_url = "https://oauth2.googleapis.com/revoke"
    if not st.session_state.get("user"):
        if cid and csec and redir:
            result = None
            had_error = False
            try:
                # Lazy import to avoid NameError if the package isn't ready yet
                from streamlit_oauth import OAuth2Component  # type: ignore
                oauth2 = OAuth2Component(cid, csec, auth_url, token_url, token_url, revoke_url)
                result = oauth2.authorize_button("Sign In with Sixdee Mail", redir, scope="openid email profile", key="google")
            except Exception:
                had_error = True
            # Show fallback link only if button failed
            if had_error:
                params = {
                    "client_id": cid,
                    "redirect_uri": redir,
                    "response_type": "code",
                    "scope": "openid email profile",
                    "access_type": "offline",
                    "prompt": "consent"
                }
                qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
                st.link_button("Sign in with Sixdee mail (fallback)", f"{auth_url}?{qs}")
            if result and isinstance(result, dict) and result.get("token"):
                try:
                    tk = result["token"]["access_token"]
                    ui = requests.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {tk}"}, timeout=10).json()
                    st.session_state["user"] = {
                        "email": ui.get("email"),
                        "name": ui.get("name") or ui.get("email"),
                        "picture": ui.get("picture"),
                        "sub": ui.get("sub"),
                    }
                    st.session_state["google_access_token"] = tk
                    # Persist to cookie
                    try:
                        if cookies is not None:
                            cookies["user"] = json.dumps(st.session_state["user"])  # type: ignore
                            cookies.save()
                    except Exception:
                        pass
                    st.rerun()
                except Exception:
                    st.error("Google login failed")
        else:
            st.error("Google login is not configured. Set google.client_id, google.client_secret and google.redirect_uri in secrets.")
    else:
        u = st.session_state["user"] or {}
        cols = st.columns([1,3,2])
        with cols[0]:
            st.markdown(f"<div class='avatar avatar-user' title='{u.get('email','')}'>{(u.get('name','?') or '?')[:1]}</div>", unsafe_allow_html=True)
        with cols[1]:
            st.markdown(f"**{u.get('name','User')}**\n\n{u.get('email','')}")
        with cols[2]:
            if st.button("Logout", use_container_width=True):
                _logout()

# -------- Auth gate: require Google login before showing the app --------
if not st.session_state.get("user"):
    # Hide sidebar and toolbar title; center a login card
    st.markdown(
        """
        <style>
          [data-testid="stSidebar"] { display: none !important; }
          div[data-testid='stToolbar']::before, div[data-testid='stToolbar']::after { content: none !important; }
          [data-testid="stAppViewContainer"] > .main { display:flex; align-items:center; justify-content:center; min-height: 96vh; background:#f5f6f8; }
          /* Suppress any debug/testid labels that might appear */
          div[data-testid='stMarkdownContainer']::before { content: none !important; }
          div[data-testid='stMarkdownContainer'] { margin-top: 0; margin-bottom: 0; }

          .login-wrap { width: 320px; max-width: 92vw; padding: 8px 0 4px; margin-left:auto; margin-right:auto; }
          .login-logo { width:68px; height:68px; border-radius:50%; background:transparent; display:flex; align-items:center; justify-content:center; margin: 8px auto 12px; box-shadow:none; }
          .login-logo img { width:36px; height:36px; display:block; }
          .login-title { font-weight:700; font-size:20px; color:#111827; margin-top:4px; text-align:center; }
          .login-right-link { font-size:12px; color:#2563eb; text-decoration:none; }
          .login-field { margin-top:10px; }
          .login-divider { height:1px; background:#e5e7eb; margin:14px auto; max-width: 320px; }
          .login-google { margin-top:8px; max-width:320px; margin-left:auto; margin-right:auto; text-align: center; }
          .login-google .stButton>button { width: 100% !important; max-width: 320px; margin: 0 auto; display:block; }
          .login-google a { display:block; width:100%; max-width:320px; margin:0 auto; }
          /* Target the OAuth button by its Streamlit key to ensure same width & centering */
          div.st-key-google { max-width:320px; margin:0 auto; width:100%; }
          div.st-key-google button { width:100% !important; max-width:320px; margin:0 auto; display:block; }
          /* Aggressive width control for nested wrappers Streamlit adds around the OAuth component */
          .login-google > div { max-width:320px !important; width:100% !important; margin:0 auto !important; box-sizing: border-box; }
          .login-google [data-testid="stHorizontalBlock"] { max-width:320px !important; width:100% !important; margin:0 auto !important; }
          .login-google [data-testid="stHorizontalBlock"] > div { max-width:320px !important; width:100% !important; margin:0 auto !important; }
          .login-google [aria-live] { max-width:320px !important; width:100% !important; margin:0 auto !important; }
          /* Make Sign In button full width and compact */
          .login-wrap .stButton>button { width:100% !important; max-width:320px; margin: 8px auto 0; display:block; }
          /* Orange filled Sign In button */
          div.st-key-login_submit .stButton>button {
            background: #F26D21 !important; /* screenshot orange */
            color: #ffffff !important;
            border: 1px solid #F26D21 !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            font-size: 15.5px !important;
            padding: 12px 14px !important; /* ~44px height */
            min-height: 44px !important;
          }
          div.st-key-login_submit .stButton>button:hover { background:#F26D21 !important; border-color:#F26D21 !important; }
          /* Google button look (official blue) */
          .login-google .stButton>button, .login-google a, .login-google button, div.st-key-google button {
            background: #4285F4 !important; /* Google blue per screenshot */
            color: #fff !important;
            border: 1px solid #4285F4 !important;
            border-radius: 6px !important;
            padding: 12px 14px 12px 56px !important; /* space for larger G badge */
            font-weight: 600 !important;
            font-size: 15.5px !important;
            min-height: 44px !important;
          }
          .login-google .stButton>button:hover, .login-google a:hover, .login-google button:hover, div.st-key-google button:hover { background: #4285F4 !important; border-color:#4285F4 !important; }
          /* Add Google badge (white square) and multi-color G icon */
          .login-google .stButton>button,
          .login-google a,
          .login-google button,
          div.st-key-google button { position: relative; overflow: visible; }
          .login-google .stButton>button::before,
          .login-google a::before,
          .login-google button::before,
          div.st-key-google button::before {
            content: "";
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            width: 34px;
            height: 34px;
            background: #ffffff;
            border-radius: 4px;
          }
          .login-google .stButton>button::after,
          .login-google a::after,
          .login-google button::after,
          div.st-key-google button::after {
            content: "";
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            width: 34px;
            height: 34px;
            background-repeat: no-repeat;
            background-position: center;
            background-size: 20px 20px;
            background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'><path fill='%23FFC107' d='M43.6 20.5h-1.9v-.1H24v7.2h11.3c-1.6 4.5-5.9 7.8-11.3 7.8-6.5 0-11.9-5.3-11.9-11.9S17.5 11.6 24 11.6c3 0 5.7 1.1 7.8 2.9l5.1-5.1C33 6.2 28.7 4.5 24 4.5 12.8 4.5 3.9 13.4 3.9 24.6S12.8 44.7 24 44.7c10.6 0 19.4-8.6 19.4-20 0-1.3-.1-2.7-.4-4.2z'/><path fill='%23FF3D00' d='M6.3 14.7l5.9 4.3C14 15.7 18.6 11.6 24 11.6c3 0 5.7 1.1 7.8 2.9l5.1-5.1C33 6.2 28.7 4.5 24 4.5 16 4.5 9 9.1 6.3 14.7z'/><path fill='%234CAF50' d='M24 44.7c5.3 0 10.2-2 13.9-5.3l-6.4-5.2c-2 1.4-4.6 2.2-7.5 2.2-5.4 0-9.9-3.4-11.5-8.1l-5.9 4.6C9.3 39.6 16 44.7 24 44.7z'/><path fill='%231976D2' d='M43.6 20.5h-1.9v-.1H24v7.2h11.3c-.8 2.3-2.3 4.3-4.4 5.7l6.4 5.2c3.7-3.4 6.1-8.4 6.1-14.7 0-1.3-.1-2.7-.4-4.2z'/></svg>");
          }
          .login-muted { text-align:center; color:#6b7280; font-size:12px; margin-top:6px; }
          /* Constrain Google auth area to same width as inputs */
          .login-google [data-testid="stHorizontalBlock"] { max-width: 320px; margin: 0 auto; }
          /* Limit width of specific login inputs and center */
          div.st-key-login_email, div.st-key-login_password { max-width: 320px; margin: 6px auto; width: 100%; }
          /* Explicitly target keyed Sign In container to center it */
          div.st-key-login_submit { max-width:320px; width:100%; margin: 8px auto 0; }
          div.st-key-login_submit .stButton>button { width:100% !important; }
          /* Google button look (official blue) */
          .login-google .stButton>button, .login-google a, .login-google button, div.st-key-google button {
            background: #4285F4 !important; /* Google blue per screenshot */
            color: #fff !important;
            border: 1px solid #4285F4 !important;
            border-radius: 6px !important;
            padding: 10px 12px 10px 52px !important; /* leave space for G badge */
            font-weight: 600 !important;
          }
          .login-google .stButton>button:hover, .login-google a:hover, .login-google button:hover, div.st-key-google button:hover { background: #4285F4 !important; border-color:#4285F4 !important; }
          /* Orange filled Sign In button */
          div.st-key-login_submit .stButton>button {
            background: #F26D21 !important; /* screenshot orange */
            color: #ffffff !important;
            border: 1px solid #F26D21 !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
          }
          div.st-key-login_submit .stButton>button:hover { background:#F26D21 !important; border-color:#F26D21 !important; }
          /* Fallback: center any Streamlit button container inside login wrap */
          .login-wrap .stButton { display:block; max-width:320px; margin: 8px auto 0; }
          /* Add 'G' badge at left like the screenshot */
          .login-google .stButton>button::before,
          .login-google a::before,
          .login-google button::before,
          div.st-key-google button::before {
            content: 'G';
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            width: 30px;
            height: 30px;
            line-height: 30px;
            text-align: center;
            color: #ffffff;
            background: #3367D6; /* darker blue badge */
            border-radius: 4px;
            font-weight: 700;
          }
          .login-google .stButton>button, .login-google a, .login-google button, div.st-key-google button {
            background: #4285F4 !important; /* Google blue per screenshot */
            color: #fff !important;
            border: 1px solid #4285F4 !important;
            border-radius: 6px !important;
            padding: 10px 12px 10px 52px !important; /* leave space for G badge */
            font-weight: 600 !important;
          }
          .login-google .stButton>button:hover, .login-google a:hover, .login-google button:hover, div.st-key-google button:hover { background: #4285F4 !important; border-color:#4285F4 !important; }
          /* Add 'G' badge at left like the screenshot */
          .login-google .stButton>button::before,
          .login-google a::before,
          .login-google button::before,
          div.st-key-google button::before {
            content: 'G';
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            width: 30px;
            height: 30px;
            line-height: 30px;
            text-align: center;
            color: #ffffff;
            background: #3367D6; /* darker blue badge */
            border-radius: 4px;
            font-weight: 700;
          }
          .login-muted { text-align:center; color:#6b7280; font-size:12px; margin-top:6px; }
          /* Constrain Google auth area to same width as inputs */
          .login-google [data-testid="stHorizontalBlock"] { max-width: 320px; margin: 0 auto; }
          /* Limit width of specific login inputs and center */
          div.st-key-login_email, div.st-key-login_password { max-width: 320px; margin: 6px auto; width: 100%; }
          /* Explicitly target keyed Sign In container to center it */
          div.st-key-login_submit { max-width:320px; width:100%; margin: 8px auto 0; }
          div.st-key-login_submit .stButton>button { width:100% !important; }
          /* Google button look (official blue) */
          .login-google .stButton>button, .login-google a, .login-google button, div.st-key-google button {
            background: #4285F4 !important; /* Google blue per screenshot */
            color: #fff !important;
            border: 1px solid #4285F4 !important;
            border-radius: 6px !important;
            padding: 10px 12px 10px 52px !important; /* leave space for G badge */
            font-weight: 600 !important;
          }
          .login-google .stButton>button:hover, .login-google a:hover, .login-google button:hover, div.st-key-google button:hover { background: #4285F4 !important; border-color:#4285F4 !important; }
          /* Orange filled Sign In button */
          div.st-key-login_submit .stButton>button {
            background: #F26D21 !important; /* screenshot orange */
            color: #ffffff !important;
            border: 1px solid #F26D21 !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
          }
          div.st-key-login_submit .stButton>button:hover { background:#F26D21 !important; border-color:#F26D21 !important; }
          /* Fallback: center any Streamlit button container inside login wrap */
          .login-wrap .stButton { display:block; max-width:320px; margin: 8px auto 0; }
          /* Explicitly target keyed Sign In container to center it */
          div.st-key-login_submit { max-width:320px; width:100%; margin: 8px auto 0; }
          div.st-key-login_submit .stButton>button { width:100% !important; }
          /* Google button look (official blue) */
          .login-google .stButton>button, .login-google a, .login-google button, div.st-key-google button {
            background: #4285F4 !important; /* Google blue per screenshot */
            color: #fff !important;
            border: 1px solid #4285F4 !important;
            border-radius: 6px !important;
            padding: 10px 12px 10px 52px !important; /* leave space for G badge */
            font-weight: 600 !important;
          }
          .login-google .stButton>button:hover, .login-google a:hover, .login-google button:hover, div.st-key-google button:hover { background: #4285F4 !important; border-color:#4285F4 !important; }
          .login-muted { text-align:center; color:#6b7280; font-size:12px; margin-top:6px; }
          /* Constrain Google auth area to same width as inputs */
          .login-google [data-testid="stHorizontalBlock"] { max-width: 320px; margin: 0 auto; }
          /* Limit width of specific login inputs and center */
          div.st-key-login_email, div.st-key-login_password { max-width: 320px; margin: 6px auto; width: 100%; }
          div.st-key-login_email [data-baseweb="input"], div.st-key-login_password [data-baseweb="input"] { max-width: 320px; margin: 0 auto; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Final overrides to ensure Google OAuth button matches Sign In size/shape
    st.markdown(
        """
        <style>
          /* Normalize Google OAuth button to match Sign In */
          .login-google .stButton>button,
          .login-google a,
          .login-google button,
          div.st-key-google button {
            font-size: 14px !important;
            font-weight: 600 !important;
            text-transform: none !important;
            padding: 12px 14px !important; /* match Sign In button */
            min-height: 44px !important;   /* match Sign In button */
            border-radius: 8px !important; /* match Sign In button */
          }
          /* Specifically make Sixdee OAuth button non-bold */
          div.st-key-google button {
            font-weight: 400 !important;
          }
          /* Remove decorative left badge to keep compact width */
          .login-google .stButton>button::before,
          .login-google a::before,
          .login-google button::before,
          div.st-key-google button::before,
          .login-google .stButton>button::after,
          .login-google a::after,
          .login-google button::after,
          div.st-key-google button::after {
            content: none !important;
            background: none !important;
            width: 0 !important;
            height: 0 !important;
            display: none !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Optional logout flash
    try:
        _qp = _read_query_params()
        if "logged_out" in _qp and ("1" in _qp.get("logged_out", [])):
            st.success("You have been logged out.")
            _clear_query_params()
    except Exception:
        pass

    # Login card UI
    st.markdown("<div class='login-card'>", unsafe_allow_html=True)
    # Top logo in orange circle using embedded data URI for reliability
    _logo_src = ""
    try:
        path = LOGO1_SVG_PATH if os.path.exists(LOGO1_SVG_PATH) else (LOGO_SVG_PATH if os.path.exists(LOGO_SVG_PATH) else None)
        if path:
            with open(path, "rb") as _f:
                _b64 = base64.b64encode(_f.read()).decode("ascii")
                _logo_src = f"data:image/svg+xml;base64,{_b64}"
    except Exception:
        _logo_src = ""
    st.markdown(f"<div class='login-logo'><img src='{_logo_src}' alt='logo'/></div>", unsafe_allow_html=True)
    # Render entire login stack inside the middle column for perfect centering
    c1, c2, c3 = st.columns([1,2,1])
    with c2:
        st.markdown("<div class='login-wrap'>", unsafe_allow_html=True)
        st.markdown("<div class='login-title'>Sign In</div>", unsafe_allow_html=True)
        st.text_input("Email or Username", key="login_email")
        st.text_input("Password", type="password", key="login_password")
        st.button("Sign In", key="login_submit", use_container_width=True)
        st.markdown("<div class='login-divider'></div>", unsafe_allow_html=True)
        st.markdown("<div class='login-google'>", unsafe_allow_html=True)
        _render_auth()
        st.markdown("</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    st.stop()

# Inject logo into the Streamlit sidebar logo spacer (native location)
if _nav_logo_rel:
    st.markdown(
        f"""
        <style>
          /* Use default st.logo image in sidebar header; ensure container has consistent height */
          div[data-testid='stLogoSpacer'] {{
            min-height: 28px;
            height: 28px;
            overflow: hidden;
          }}
          /* On very narrow viewports (proxy for collapsed), keep navbar logo consistent */
          @media (max-width: 900px) {{
            div[data-testid='stLogoSpacer'] {{
              background-image: url('{_nav_logo_rel}') !important;
              background-repeat: no-repeat;
              background-position: 6px center;
              background-size: 18px auto;
              height: 24px;
              min-height: 24px;
            }}
          }}
          /* Add spacing under the sidebar header so first content (e.g., product list) is not tight */
          header[data-testid='stSidebarHeader'] + div,
          div[data-testid='stSidebarHeader'] + div {{
            margin-top: 8px !important;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )

def _render_create_form(prefix: str = "dialog"):
    # Only admins (by email list) can create — no password fallback here
    if not is_admin_user():
        st.info("Admin access required to create products.")
        return
    with st.form(f"product_form_{prefix}"):
        name = st.text_input("Knowledgebase name", key=f"name_{prefix}")
        desc = st.text_area("Knowledgebase description", key=f"desc_{prefix}")
        pdf_file = st.file_uploader("Upload PDF", type=["pdf"], key=f"pdf_{prefix}")
        submitted = st.form_submit_button("Create/Update Knowledgebase")
    if submitted:
        if not name:
            st.warning("Please enter a Knowledgebase name.")
        elif not pdf_file:
            st.warning("Please upload a product PDF.")
        else:
            product = store.get_by_name(name)
            if product is None:
                product_id = str(uuid.uuid4())
            else:
                product_id = product["id"]
            pdf_path = os.path.join(PDF_DIR, f"{product_id}.pdf")
            with open(pdf_path, "wb") as f:
                f.write(pdf_file.read())
            try:
                text = extract_text_from_pdf(pdf_path)
                chunks = chunk_text(text)
            except Exception as e:
                st.error(f"Failed to process PDF: {e}")
                chunks = []
            # Removed: store.upsert() - using database only
            retriever.index_product(product_id, chunks)
            st.success("Knowledgebase saved and indexed successfully.")

# Create Knowledgebase dialog (admin protected) with fallback
if st.session_state.get("show_create_dialog"):
    if hasattr(st, "dialog"):
        with st.dialog("Create Knowledgebase"):
            _render_create_form("dialog")
            cols = st.columns([1, 1, 1])
            if cols[2].button("Close"):
                st.session_state["show_create_dialog"] = False
    else:
        with st.sidebar:
            st.subheader("Create Knowledgebase")
            _render_create_form("sidebar")
            if st.button("Close"):
                st.session_state["show_create_dialog"] = False

# Footer logo removed per request

# Ensure a safe default for the current page before rendering sidebar/menu
page = st.session_state.get("nav_page", "Dashbord")
if page == "aarya":
    page = "Aarya"

# Minimal sidebar navigation (clean, no captions)
with st.sidebar:
    _options = ["Dashbord", "Aarya"]
    _icons = ["speedometer2", "chat-dots"]
    if is_admin_user():
        _options.append("Knowledgebase")
        _icons.append("folder-plus")
    _default_index = _options.index(page) if page in _options else 0
    try:
        side_selected = option_menu(
            menu_title=None,
            options=_options,
            icons=_icons,
            menu_icon="list",
            default_index=_default_index,
            styles={
                "container": {
                    "padding": "0",
                    "background-color": "var(--sidebar-bg)",
                    "margin": "0",
                    "border": "0",
                    "box-shadow": "none",
                },
                "icon": {"color": "#2563eb", "font-size": "16px"},
                "nav-link": {"font-size": "14px", "padding": "8px 12px", "border-radius": "10px", "margin": "2px 0"},
                "nav-link-selected": {"background-color": "#eaf2ff", "color": "#111827"},
            },
            key="sidebar_menu",
        )
        if side_selected != page:
            # Clear relevant UI state and caches on navigation
            for k in ["kb_selected_rows", "kb_pg_ed", "kb_search", "kb_ps_ed"]:
                try:
                    if k in st.session_state:
                        del st.session_state[k]
                except Exception:
                    pass
            try:
                st.cache_data.clear()
            except Exception:
                pass
            try:
                st.cache_resource.clear()
            except Exception:
                pass
            st.session_state["nav_page"] = side_selected
            page = side_selected
    except Exception:
        pass

# Ensure 'page' is defined even if sidebar failed to render option_menu
if 'page' not in locals() or not page:
    page = st.session_state.get("nav_page", "Aarya")

# Toolbar user chip (Google style) on the right
if st.session_state.get("user"):
    u = st.session_state.get("user", {})
    name_html = (u.get("name") or u.get("email") or "User").replace("<", "&lt;").replace(">", "&gt;")
    initial = (u.get("name") or u.get("email") or "?")[:1]
    pic = u.get("picture") or ""
    prof_on = bool(st.session_state.get("show_profile"))
    st.markdown(
        """
        <style>
          .tb-chip { position: fixed; top: 8px; right: 16px; z-index: 2147483647; display:flex; align-items:center; gap:10px; }
          .tb-chip .chip { display:flex; align-items:center; gap:8px; background:#fff; border:1px solid #e5e7eb; border-radius:9999px; padding:4px 10px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); }
          .tb-chip .avatar { width:24px; height:24px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:13px; background:#e5e7eb; color:#374151; overflow:hidden; }
          .tb-chip .avatar img { width:100%; height:100%; object-fit: cover; display:block; }
          .tb-chip .name span { font-size:13px; color:#111827; text-decoration:none; max-width: 180px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display:block; }
          /* Profile dropdown */
          .tb-profile { position: fixed; top: 44px; right: 16px; z-index: 2147483651; background:#fff; border:1px solid #e5e7eb; border-radius:12px; box-shadow:0 8px 24px rgba(0,0,0,0.12); min-width: 260px; max-width: 320px; overflow:hidden; }
          .tb-profile .row { display:flex; align-items:center; gap:12px; padding: 12px 14px; }
          .tb-profile .row + .row { border-top: 1px solid #f1f5f9; }
          .tb-profile .avatar-xl { width:40px; height:40px; border-radius:50%; background:#e5e7eb; color:#374151; display:flex; align-items:center; justify-content:center; font-size:16px; overflow:hidden; }
          .tb-profile .avatar-xl img { width:100%; height:100%; object-fit: cover; display:block; }
          .tb-profile .name { font-weight:600; color:#111827; }
          .tb-profile .email { font-size:12px; color:#6b7280; }
          .tb-profile .logout-btn { margin-left:auto; color:#ef4444; text-decoration:none; font-size:13px; }
          .tb-profile .logout-btn:hover { text-decoration: underline; }
          .tb-profile, .tb-profile * { pointer-events: auto !important; }
          /* Transparent button overlay positioned over the chip to catch clicks */
          div.st-key-chip_toggle_btn { position: fixed; top: 8px; right: 16px; width: 200px; height: 36px; z-index: 2147483650; }
          div.st-key-chip_toggle_btn button { width: 100%; height: 100%; background: transparent !important; border: 0 !important; color: transparent !important; box-shadow: none !important; }
          /* Close button overlay when dropdown is open */
          div.st-key-chip_close_btn { position: fixed; top: 8px; right: 16px; width: 200px; height: 36px; z-index: 2147483650; }
          div.st-key-chip_close_btn button { width: 100%; height: 100%; background: transparent !important; border: 0 !important; color: transparent !important; box-shadow: none !important; }
          /* No extra visible logout button; handled via JS click on the text */
        </style>
        """,
        unsafe_allow_html=True,
    )

    avatar_small_html = f"<img src='{pic}' alt='avatar'/>" if pic else initial
    avatar_large_html = f"<img src='{pic}' alt='avatar'/>" if pic else initial

    # Native details/summary dropdown for seamless menu behavior
    menu_html = (
        "<details class='tb-profile-menu' id='tb-profile-menu'>"
        "  <summary>"
        "    <div class='chip' aria-haspopup='menu' aria-expanded='false'>"
        f"      <div class='avatar' title='{u.get('email','')}'>{avatar_small_html}</div>"
        f"      <div class='name'><span>{name_html}</span></div>"
        "    </div>"
        "  </summary>"
        "  <div class='menu' role='menu'>"
        "    <div class='row'>"
        f"      <div class='avatar-xl'>{avatar_large_html}</div>"
        "      <div style='min-width:0'>"
        f"        <div class='name'>{name_html}</div>"
        f"        <div class='email'>{u.get('email','')}</div>"
        "      </div>"
        "      <a class='logout-btn' href='./?logout=1' target='_self' role='menuitem'>Logout</a>"
        "    </div>"
        "  </div>"
        "</details>"
        "<script>"
        "  (function(){"
        "    try {"
        "      const d = document.getElementById('tb-profile-menu');"
        "      if (!d) return;"
        "      // Keep aria-expanded in sync"
        "      d.addEventListener('toggle', function(){"
        "        const chip = d.querySelector('.chip');"
        "        if (chip) chip.setAttribute('aria-expanded', d.open ? 'true' : 'false');"
        "      });"
        "      // Toggle on chip click to avoid any event interference"
        "      const chip = d.querySelector('.chip');"
        "      if (chip) { chip.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); d.open = !d.open; }); }"
        "      // Close on outside click"
        "      document.addEventListener('click', function(e){ if (!d.open) return; if (!d.contains(e.target)) { d.open = false; } }, true);"
        "      // Close on Escape"
        "      document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && d.open) { d.open = false; } }, true);"
        "    } catch(e) {}"
        "  })();"
        "</script>"
    )

    # Render native menu
    st.markdown(menu_html, unsafe_allow_html=True)
    # No extra visible logout controls; link navigates with ?logout=1 which triggers server _logout()

# ---------------------- Knowledgebase Page ----------------------
if page == "Knowledgebase":
    # Update toolbar title for this page
    st.markdown(
        """
        <style>
          div[data-testid='stToolbar']::after { content: 'Knowledgebase'; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if not is_admin_user():
        st.info("This page is restricted to admins.")
    else:
        # Manage knowledge base (Edit/Delete only)
        repo = get_pdf_metadata_repo()
        products = _enrich_rows_with_status_label(repo.list_all())
        # Create/Edit appears before the table; driven by previously selected rows (from session_state)
        sel_rows_state = st.session_state.get("kb_selected_rows", [])
        is_edit_state = len(sel_rows_state) == 1
        st.markdown(f"**{'Edit Knowledgebase' if is_edit_state else 'Create Knowledgebase'}**")
        with st.container():
            if is_edit_state:
                _pid = sel_rows_state[0].get("id")
                _cur = next((p for p in products if p.get("id") == _pid), None) or {}
                
                # Card-style container with professional styling
                st.markdown("""
                    <style>
                        .kb-edit-card {
                            background: #f8f9fa;
                            border: 1px solid #e0e0e0;
                            border-radius: 8px;
                            padding: 20px;
                            margin-bottom: 20px;
                        }
                        .kb-file-info {
                            background: white;
                            border: 1px solid #e0e0e0;
                            border-radius: 6px;
                            padding: 12px;
                            margin: 10px 0;
                        }
                        .kb-file-name {
                            font-size: 14px;
                            color: #333;
                            font-weight: 500;
                            margin-bottom: 8px;
                        }
                        .kb-metadata {
                            font-size: 12px;
                            color: #666;
                            padding: 12px;
                            background: #f8f9fa;
                            border-radius: 6px;
                            margin-top: 15px;
                            margin-bottom: 15px;
                        }
                        .kb-file-row {
                            background: white;
                            border: 1px solid #e0e0e0;
                            border-radius: 6px;
                            padding: 8px 12px;
                            margin: 8px 0;
                        }
                    </style>
                """, unsafe_allow_html=True)
                
                name_val = st.text_input("Name", value=_cur.get("name", ""), key=f"kb_ce_name_{_pid}")
                desc_val = st.text_area("Description", value=_cur.get("description", ""), key=f"kb_ce_desc_{_pid}")
                
                # Upload new files (multiple)
                new_pdfs = st.file_uploader("Upload PDF files (optional)", type=["pdf"], key=f"kb_ce_edit_pdf_{_pid}", accept_multiple_files=True)
                
                # Current file section with card styling - shown after upload
                cur_fname = os.path.basename(_cur.get("pdf_path", "")) if _cur.get("pdf_path") else "-"
                pdf_path = _cur.get("pdf_path", "")
                
                if pdf_path and os.path.exists(pdf_path):
                    st.markdown("<label style='font-size: 14px; font-weight: 400; color: rgb(49, 51, 63); margin-bottom: 0.25rem; display: block;'>Current Files</label>", unsafe_allow_html=True)
                    
                    # Get file size
                    try:
                        file_size = os.path.getsize(pdf_path)
                        file_size_mb = file_size / (1024 * 1024)
                    except:
                        file_size_mb = 0
                    
                    # Use container with custom styling
                    with st.container():
                        st.markdown("""
                            <style>
                            /* Remove borders from file row container */
                            div[data-testid="stHorizontalBlock"] {
                                background: white;
                                border: 1px solid #e0e0e0;
                                border-radius: 6px;
                                padding: 8px 12px;
                                margin: 8px 0;
                            }
                            /* Remove borders from buttons in current files section */
                            .kb-current-file-btn button {
                                background: transparent !important;
                                border: none !important;
                                box-shadow: none !important;
                                padding: 4px 8px !important;
                            }
                            .kb-current-file-btn button:hover {
                                opacity: 0.7 !important;
                                border: none !important;
                            }
                            .kb-current-file-btn button:focus {
                                border: none !important;
                                box-shadow: none !important;
                                outline: none !important;
                            }
                            </style>
                        """, unsafe_allow_html=True)
                        
                        # Horizontal layout: icon + filename + size + delete + view
                        col_icon, col_name, col_size, col_delete, col_view = st.columns([0.3, 3, 0.8, 0.5, 0.5])
                        
                        with col_icon:
                            st.markdown("<div style='padding-top: 8px;'>📄</div>", unsafe_allow_html=True)
                        
                        with col_name:
                            st.markdown(f"<div style='padding-top: 8px; font-size: 14px; color: #333;'>{cur_fname}</div>", unsafe_allow_html=True)
                        
                        with col_size:
                            st.markdown(f"<div style='padding-top: 8px; font-size: 12px; color: #999;'>{file_size_mb:.2f} MB</div>", unsafe_allow_html=True)
                        
                        with col_delete:
                            st.markdown('<div class="kb-current-file-btn">', unsafe_allow_html=True)
                            if st.button("✕", key=f"kb_delete_pdf_{_pid}", help="Remove PDF file reference"):
                                # Just clear the pdf_path reference without deleting the actual file
                                try:
                                    repo = get_pdf_metadata_repo()
                                    repo.update(_pid, {"pdf_path": ""})
                                    st.success("PDF file reference removed successfully.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed to remove file reference: {e}")
                            st.markdown('</div>', unsafe_allow_html=True)

                        with col_view:
                            st.markdown('<div class="kb-current-file-btn">', unsafe_allow_html=True)
                            with open(pdf_path, "rb") as f:
                                pdf_bytes = f.read()
                            st.download_button(
                                label="👁️",
                                data=pdf_bytes,
                                file_name=cur_fname,
                                mime="application/pdf",
                                key=f"kb_preview_{_pid}",
                                use_container_width=False,
                                help="View/Download PDF"
                            )
                            st.markdown('</div>', unsafe_allow_html=True)
                
                # Metadata section
                _c_by = _cur.get("created_by") or "-"
                _c_at = _cur.get("created_at") or "-"
                _u_by = _cur.get("updated_by") or "-"
                _u_at = _cur.get("updated_at") or "-"
                st.markdown(
                    f"<div class='kb-metadata'>Created by <b>{_c_by}</b> on <b>{_c_at}</b><br>Last updated by <b>{_u_by}</b> on <b>{_u_at}</b></div>",
                    unsafe_allow_html=True,
                )
                
                st.markdown("<br>", unsafe_allow_html=True)
                cols_actions = st.columns([1, 6, 1])
                with cols_actions[0]:
                    if st.button("Clear", key=f"kb_ce_clear_{_pid}"):
                        try:
                            for _k in [f"kb_ce_name_{_pid}", f"kb_ce_desc_{_pid}", f"kb_ce_edit_pdf_{_pid}"]:
                                if _k in st.session_state:
                                    del st.session_state[_k]
                            # Switch to create mode
                            st.session_state["kb_selected_rows"] = []
                            # Also clear any create form remnants
                            for _k in ["kb_ce_new_name", "kb_ce_new_desc", "kb_ce_new_pdf"]:
                                if _k in st.session_state:
                                    del st.session_state[_k]
                            # Clear any table selections from data editor
                            if "kb_inline_de" in st.session_state:
                                del st.session_state["kb_inline_de"]
                        except Exception:
                            pass
                        st.rerun()
                with cols_actions[2]:
                    if st.button("Save changes", key=f"kb_ce_save_{_pid}"):
                        if (
                            name_val != _cur.get("name") or
                            desc_val != _cur.get("description") or
                            new_pdfs
                        ):
                            user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                            # If new PDFs were uploaded, process them
                            pdf_path = _cur.get("pdf_path", "")
                            if new_pdfs and pdf_path:
                                try:
                                    # Process the first PDF file (or merge multiple if needed)
                                    # For now, we'll use the first file
                                    first_pdf = new_pdfs[0]
                                    _bytes = first_pdf.read()
                                    with open(pdf_path, "wb") as f:
                                        f.write(_bytes)
                                    try:
                                        upload_url = (str(
                                            (CONFIG_TOML.get("custom", {}) or {}).get("UPLOAD_WEBHOOK_URL")
                                            or CONFIG_TOML.get("UPLOAD_WEBHOOK_URL")
                                            or ((st.secrets.get("configurl") if hasattr(st, "secrets") else None))
                                            or st.secrets.get("upload_url")
                                            or os.environ.get("CONFIG_URL")
                                            or os.environ.get("UPLOAD_WEBHOOK_URL")
                                            or ""
                                        )).strip()
                                    except Exception:
                                        upload_url = ""
                                    if not upload_url:
                                        st.warning("Upload webhook URL not configured; delete webhook not sent.")
                                    if upload_url:
                                        try:
                                            pass
                                        except Exception:
                                            st.warning("Failed to call upload webhook")
                                    try:
                                        text = extract_text_from_pdf(pdf_path)
                                        chunks = chunk_text(text)
                                    except Exception:
                                        chunks = []
                                    try:
                                        retriever.index_product(_pid, chunks)
                                    except Exception:
                                        pass
                                    if len(new_pdfs) > 1:
                                        st.info(f"Note: {len(new_pdfs)} files selected, but only the first file was processed.")
                                except Exception:
                                    pass
                            # Always notify webhook for edit (with or without a new file)
                            try:
                                upload_url = (str(
                                    (CONFIG_TOML.get("custom", {}) or {}).get("UPLOAD_WEBHOOK_URL")
                                    or CONFIG_TOML.get("UPLOAD_WEBHOOK_URL")
                                    or ((st.secrets.get("configurl") if hasattr(st, "secrets") else None))
                                    or st.secrets.get("upload_url")
                                    or os.environ.get("CONFIG_URL")
                                    or os.environ.get("UPLOAD_WEBHOOK_URL")
                                    or ""
                                )).strip()
                            except Exception:
                                upload_url = ""
                            if upload_url:
                                try:
                                    try:
                                        fname = getattr(new_pdfs[0], "name", None) if new_pdfs else None
                                    except Exception:
                                        fname = None
                                    if not fname:
                                        try:
                                            fname = os.path.basename(pdf_path) or "uploaded.pdf"
                                        except Exception:
                                            fname = "uploaded.pdf"
                                    file_bytes = None
                                    try:
                                        with open(pdf_path, "rb") as _f:
                                            file_bytes = _f.read()
                                    except Exception:
                                        file_bytes = None
                                    _data = {
                                        "id": _pid,
                                        "name": name_val,
                                        "operation": "edit",
                                        "description": desc_val,
                                        "pdf_path": pdf_path,
                                        "created_by": _cur.get("created_by", ""),
                                        "created_at": _cur.get("created_at", ""),
                                        "updated_by": user_email,
                                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                                    }
                                    if file_bytes is not None:
                                        _resp = requests.post(
                                            upload_url,
                                            data=_data,
                                            files={"file": (fname, file_bytes, "application/pdf")},
                                            timeout=20,
                                        )
                                    else:
                                        _resp = requests.post(
                                            upload_url,
                                            data=_data,
                                            timeout=20,
                                        )
                                    try:
                                        _j = _resp.json()
                                        if bool(_j.get("success")):
                                            st.toast(str(_j.get("message") or "File uploaded successfully"), icon="✅")
                                        else:
                                            st.toast(str(_j.get("message") or "Upload webhook did not confirm success"), icon="⚠️")
                                    except Exception:
                                        st.toast("Upload webhook responded without JSON", icon="⚠️")
                                except Exception:
                                    st.toast("Failed to call upload webhook", icon="⚠️")
                            # Removed: store.upsert() - using database only
                            st.toast("Saved changes.", icon="✅")
                            st.rerun()
                        else:
                            st.toast("No changes detected.", icon="ℹ️")
            else:
                name_c = st.text_input("Knowledgebase name", key="kb_ce_new_name")
                desc_c = st.text_input("Short description", key="kb_ce_new_desc")
                pdf_c = st.file_uploader("Upload PDF", type=["pdf"], key="kb_ce_new_pdf")
                cols_actions_new = st.columns([1, 6, 1])
                with cols_actions_new[0]:
                    if st.button("Clear", key="kb_ce_new_clear"):
                        try:
                            for _k in ["kb_ce_new_name", "kb_ce_new_desc", "kb_ce_new_pdf"]:
                                if _k in st.session_state:
                                    del st.session_state[_k]
                            # Ensure we are in create mode
                            st.session_state["kb_selected_rows"] = []
                            # Clear any table selections from data editor
                            if "kb_inline_de" in st.session_state:
                                del st.session_state["kb_inline_de"]
                        except Exception:
                            pass
                        st.rerun()
                with cols_actions_new[2]:
                    if st.button("Create", key="kb_ce_new_submit"):
                        if not name_c:
                            st.toast("Please enter a Knowledgebase name.", icon="⚠️")
                        elif not pdf_c:
                            st.toast("Please upload a product PDF.", icon="⚠️")
                        else:
                            pid = str(uuid.uuid4())
                            pdf_path = os.path.join(PDF_DIR, f"{pid}.pdf")
                            _bytes = pdf_c.read()
                            with open(pdf_path, "wb") as f:
                                f.write(_bytes)
                            try:
                                upload_url = (str(
                                    (CONFIG_TOML.get("custom", {}) or {}).get("UPLOAD_WEBHOOK_URL")
                                    or CONFIG_TOML.get("UPLOAD_WEBHOOK_URL")
                                    or ((st.secrets.get("configurl") if hasattr(st, "secrets") else None))
                                    or st.secrets.get("upload_url")
                                    or os.environ.get("CONFIG_URL")
                                    or os.environ.get("UPLOAD_WEBHOOK_URL")
                                    or ""
                                )).strip()
                            except Exception:
                                upload_url = ""
                            if upload_url:
                                try:
                                    _fname = getattr(pdf_c, "name", "uploaded.pdf")
                                    user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                                    _resp = requests.post(
                                        upload_url,
                                        data={
                                            "id": pid,
                                            "name": name_c,
                                            "operation": "create",
                                            "description": (desc_c or ""),
                                            "pdf_path": pdf_path,
                                            "created_by": user_email,
                                            "created_at": datetime.now().isoformat(timespec="seconds"),
                                            "updated_by": "",
                                            "updated_at": "",
                                        },
                                        files={"file": (_fname, _bytes, "application/pdf")},
                                        timeout=20,
                                    )
                                    try:
                                        _j = _resp.json()
                                        if bool(_j.get("success")):
                                            st.toast(str(_j.get("message") or "File uploaded successfully"), icon="✅")
                                        else:
                                            st.toast(str(_j.get("message") or "Upload webhook did not confirm success"), icon="⚠️")
                                    except Exception:
                                        st.toast("Upload webhook responded without JSON", icon="⚠️")
                                except Exception:
                                    st.toast("Failed to call upload webhook", icon="⚠️")
                            try:
                                text = extract_text_from_pdf(pdf_path)
                                chunks = chunk_text(text)
                            except Exception as e:
                                st.toast(f"Failed to process PDF: {e}", icon="❌")
                                chunks = []
                            user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                            emails_list: List[str] = [user_email] if user_email else []
                            # Removed: store.upsert() - using database only
                            retriever.index_product(pid, chunks)
                            st.toast("Knowledgebase created and indexed successfully.", icon="✅")
                            st.rerun()

        # Inline editing table using Streamlit Data Editor
        if products:
            st.markdown("<hr style='margin: 24px 0 16px 0; border: none; border-top: 1px solid #e5e7eb;'>", unsafe_allow_html=True)
            st.markdown(
                """
                <style>
                  .kb-header { margin-bottom: 8px !important; padding-bottom: 0 !important; }
                </style>
                <div class="kb-header"><strong>Existing knowledges</strong></div>
                """,
                unsafe_allow_html=True
            )
            rows = []
            for p in products:
                rows.append({
                    "Knowledgebase name": p.get("name", ""),
                    "Description": p.get("description", ""),
                    "Created by": p.get("created_by", ""),
                    "Created at": p.get("created_at", ""),
                    "Updated by": p.get("updated_by", ""),
                    "Updated at": p.get("updated_at", ""),
                    "Select": False,
                    "_id": p.get("id")
                })
            df = pd.DataFrame(rows)
            # Include hidden _id column to keep a stable row identity even if user sorts in the editor
            # Place 'Select' as the first column as requested
            # ID column is kept in dataframe but hidden from display
            display_cols = ["Select", "Knowledgebase name", "Description", "Created by", "Created at", "Updated by", "Updated at", "_id"]
            if df.empty:
                st.info("No Knowledgebases available.")
            else:
                # Place a container BEFORE the table to render the action bar above the table header
                ab_container = st.container()
                edited = st.data_editor(
                    df[display_cols],
                    key="kb_inline_de",
                    hide_index=True,
                    width='stretch',
                    column_config={
                        "Knowledgebase name": st.column_config.TextColumn(disabled=False),
                        "Description": st.column_config.TextColumn(disabled=False),
                        "Created by": st.column_config.TextColumn(disabled=True),
                        "Created at": st.column_config.TextColumn(disabled=True),
                        "Updated by": st.column_config.TextColumn(disabled=True),
                        "Updated at": st.column_config.TextColumn(disabled=True),
                        "Select": st.column_config.CheckboxColumn(help="Select one row to enable Edit"),
                        "_id": None,  # Hide ID column by default
                    },
                )
                # Render the action bar INTO the container we placed before the table
                with ab_container:
                    st.markdown(
                        """
                        <style>
                          /* Remove borders from column containers */
                          [data-testid="stHorizontalBlock"] { 
                            gap: 0 !important; 
                            padding: 0 !important; 
                            margin: 0 !important; 
                            border: none !important;
                            box-shadow: none !important;
                          }
                          [data-testid="column"] { 
                            padding: 0 !important; 
                            margin: 0 !important; 
                            border: none !important;
                            box-shadow: none !important;
                          }
                          .element-container { 
                            padding: 0 !important; 
                            margin: 0 !important; 
                            border: none !important;
                          }
                          /* Target Streamlit layout wrapper divs to remove default spacing */
                          .kb-action-btn [data-testid="stHorizontalBlock"] { gap: 0 !important; padding: 0 !important; margin: 0 !important; border: none !important; }
                          .kb-action-btn [data-testid="column"] { padding: 0 !important; margin: 0 !important; border: none !important; }
                          .kb-action-btn .element-container { padding: 0 !important; margin: 0 !important; border: none !important; }
                          .kb-action-btn [class*="stLayoutWrapper"] { padding: 0 !important; margin: 0 !important; border: none !important; }
                          /* Remove all spacing from action button containers */
                          .kb-action-btn { margin: 0 !important; padding: 0 !important; border: none !important; }
                          .kb-action-btn .stButton { margin: 0 !important; padding: 0 !important; border: none !important; }
                          .kb-action-btn .stButton>button {
                            background: transparent !important;
                            background-color: transparent !important;
                            border: none !important;
                            border-width: 0 !important;
                            box-shadow: none !important;
                            padding: 0 !important;
                            margin: 0 !important;
                            min-height: auto !important;
                            border-radius: 0 !important;
                            color: #475569 !important;
                            font-size: 18px !important;
                            line-height: 1 !important;
                          }
                          .kb-action-btn .stButton>button:hover { 
                            background: transparent !important; 
                            background-color: transparent !important;
                            opacity: 0.7 !important; 
                            border: none !important;
                            border-width: 0 !important;
                          }
                          .kb-action-btn .stButton>button:active { 
                            background: transparent !important; 
                            background-color: transparent !important;
                            border: none !important;
                            border-width: 0 !important;
                          }
                          .kb-action-btn .stButton>button:focus { 
                            background: transparent !important; 
                            background-color: transparent !important;
                            outline: none !important; 
                            box-shadow: none !important; 
                            border: none !important;
                            border-width: 0 !important;
                          }
                          .kb-action-btn .stButton>button:disabled { opacity: 0.3; cursor: not-allowed; }
                          /* Target buttons by key for extra specificity */
                          div.st-key-kb_inline_delete_sel,
                          div.st-key-kb_inline_edit_sel {
                            border: none !important;
                            box-shadow: none !important;
                          }
                          div.st-key-kb_inline_delete_sel button,
                          div.st-key-kb_inline_edit_sel button {
                            background: transparent !important;
                            background-color: transparent !important;
                            border: none !important;
                            border-width: 0 !important;
                            box-shadow: none !important;
                          }
                          div.st-key-kb_inline_delete_sel button:hover,
                          div.st-key-kb_inline_edit_sel button:hover,
                          div.st-key-kb_inline_delete_sel button:active,
                          div.st-key-kb_inline_edit_sel button:active,
                          div.st-key-kb_inline_delete_sel button:focus,
                          div.st-key-kb_inline_edit_sel button:focus {
                            background: transparent !important;
                            background-color: transparent !important;
                            border: none !important;
                            border-width: 0 !important;
                            box-shadow: none !important;
                            outline: none !important;
                          }
                        </style>
                        """,
                        unsafe_allow_html=True,
                    )
                    # Use columns to force right alignment
                    spacer, btn_del, btn_edit = st.columns([0.92, 0.04, 0.04])
                    with btn_del:
                        st.markdown('<div class="kb-action-btn">', unsafe_allow_html=True)
                        if st.button("🗑️", disabled=(len(edited.loc[edited["Select"] == True, "_id"].tolist()) == 0), key="kb_inline_delete_sel", help="Delete selected"):
                            del_count = 0
                            for pid in edited.loc[edited["Select"] == True, "_id"].tolist():
                                # Call delete webhook (JSON body)
                                try:
                                    upload_url = (str(
                                        (CONFIG_TOML.get("custom", {}) or {}).get("UPLOAD_WEBHOOK_URL")
                                        or CONFIG_TOML.get("UPLOAD_WEBHOOK_URL")
                                        or ((st.secrets.get("configurl") if hasattr(st, "secrets") else None))
                                        or st.secrets.get("upload_url")
                                        or os.environ.get("CONFIG_URL")
                                        or os.environ.get("UPLOAD_WEBHOOK_URL")
                                        or ""
                                    )).strip()
                                except Exception:
                                    upload_url = ""
                                if not upload_url:
                                    st.toast("Upload webhook URL not configured; delete webhook not sent.", icon="⚠️")
                                if upload_url:
                                    try:
                                        cur = None
                                        try:
                                            cur = next((p for p in products if p.get("id") == pid), None) or store.get(pid)
                                        except Exception:
                                            cur = None
                                        user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                                        pdfp = (cur or {}).get("pdf_path", "") or os.path.join(PDF_DIR, f"{pid}.pdf")
                                        _payload = {
                                            "id": pid,
                                            "name": (cur or {}).get("name", ""),
                                            "operation": "delete",
                                            "description": (cur or {}).get("description", ""),
                                            "pdf_path": pdfp,
                                            "created_by": (cur or {}).get("created_by", ""),
                                            "created_at": (cur or {}).get("created_at", ""),
                                            "updated_by": user_email,
                                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                                        }
                                        st.info(f"Calling delete webhook: {upload_url}")
                                        _r = requests.post(upload_url, json=_payload, timeout=10)
                                        try:
                                            _j = _r.json()
                                            if not bool(_j.get("success")):
                                                st.warning(str(_j.get("message") or "Delete webhook did not confirm success"))
                                        except Exception:
                                            st.warning(f"Delete webhook responded without JSON (status {getattr(_r,'status_code',None)}): {getattr(_r,'text','')[:200]}")
                                    except Exception:
                                        st.warning("Failed to call delete webhook for selected row")
                                # Removed: store.delete(pid) - using database only
                                del_count += 1
                                try:
                                    pdfp = os.path.join(PDF_DIR, f"{pid}.pdf")
                                    if os.path.exists(pdfp): os.remove(pdfp)
                                except Exception:
                                    pass
                                try:
                                    chunkp = os.path.join(TEXT_DIR, f"{pid}.json")
                                    if os.path.exists(chunkp): os.remove(chunkp)
                                except Exception:
                                    pass
                            if del_count:
                                st.success(f"Deleted {del_count} row(s).")
                            else:
                                st.info("No rows deleted.")
                            st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)
                    with btn_edit:
                        st.markdown('<div class="kb-action-btn">', unsafe_allow_html=True)
                        if st.button("✏️", disabled=(len(edited.loc[edited["Select"] == True, "_id"].tolist()) != 1), key="kb_inline_edit_sel", help="Edit selected"):
                            pid = edited.loc[edited["Select"] == True, "_id"].tolist()[0]
                            st.session_state["kb_selected_rows"] = [{"id": pid}]
                            st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)

                # Auto-apply inline edits immediately (compare edited vs original df by _id)
                try:
                    original_by_id = {str(r["_id"]): r for _, r in df.iterrows()}
                    updated_count = 0
                    for _, row in edited.iterrows():
                        pid = str(row.get("_id") or "")
                        if not pid:
                            continue
                        cur = original_by_id.get(pid)
                        if not cur:
                            continue
                        new_name = str(row.get("Knowledgebase name") or "")
                        new_desc = str(row.get("Description") or "")
                        if new_name != (cur.get("Knowledgebase name") or "") or new_desc != (cur.get("Description") or ""):
                            cur_store = next((p for p in products if p.get("id") == pid), None)
                            user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                            # Removed: store.upsert() - using database only
                            updated_count += 1
                    if updated_count:
                        st.success(f"Updated {updated_count} row(s).")
                        st.rerun()
                except Exception:
                    pass

# ---------------------- Dashbord Page ----------------------
elif page == "Dashbord":

    Dashboard().render()

# ---------------------- aarya Page ----------------------
elif page == "Aarya":

    repo = get_pdf_metadata_repo()
    products = _enrich_rows_with_status_label(repo.list_all())
    if not products:
        st.info("No products available. An admin must create one first.")
    else:
        name_to_id = {p["name"]: p["id"] for p in products}
        
        # Auto-initialize WebSocket connection on page load
        if "aarya_session_id" not in st.session_state:
            st.session_state["aarya_session_id"] = f"session_{uuid.uuid4().hex[:8]}_{int(time.time()*1000)}"
        if "aarya_client_id" not in st.session_state:
            st.session_state["aarya_client_id"] = f"selfcare_{int(time.time()*1000)}_{str(uuid.uuid4().int)[-3:]}"
        
        # Reduce spacing at top of page
        st.markdown(
            """
            <style>
              /* Reduce top spacing for Aarya page */
              div[data-testid="stVerticalBlock"] > div:first-child {
                padding-top: 0 !important;
                margin-top: 0 !important;
              }
              div[data-testid="stVerticalBlock"] > div {
                padding-top: 0 !important;
                margin-top: 0 !important;
              }
              /* Reduce spacing between elements */
              .element-container {
                margin-bottom: 0.25rem !important;
                padding-bottom: 0 !important;
              }
              /* Reconnect button alignment and styles (global) */
              div.st-key-reconnect_btn {
                margin-top: 0 !important;
                margin-left: 10px !important;
                display: flex;
                justify-content: flex-end;
                align-items: center;
                height: 40px !important; /* match select height */
              }
              /* Wrapper to ensure no extra element pushes the button down */
              div.st-key-reconnect_btn_wrap {
                display: flex !important;
                align-items: center !important;
                justify-content: flex-end !important;
                height: 40px !important; /* match select height */
                margin: 0 !important;
                padding: 0 !important;
              }
              div.st-key-reconnect_btn button,
              div.st-key-reconnect_btn button[kind="primary"],
              div.st-key-reconnect_btn button[kind="secondary"] {
                border-radius: 9999px !important;
                padding: 0 !important;
                min-height: 40px !important;
                height: 40px !important;
                width: 40px !important;
                min-width: 40px !important;
                display: inline-flex !important;
                align-items: center !important;
                justify-content: center !important;
                line-height: 1 !important;
                font-size: 16px !important;
                box-shadow: none !important;
                border-width: 1px !important;
                transition: background-color .15s ease, border-color .15s ease, color .15s ease;
                margin-top: -1px !important; /* optical centering */
              }
              div.st-key-reconnect_btn button[kind="secondary"] {
                background: #f3f4f6 !important; /* gray-100 */
                background-color: #f3f4f6 !important;
                border-color: #e5e7eb !important; /* gray-200 */
                color: #6b7280 !important; /* gray-500 */
              }
              div.st-key-reconnect_btn button[kind="secondary"]:hover {
                background: #f3f4f6 !important;
                background-color: #f3f4f6 !important;
                border-color: #d1d5db !important;
                color: #374151 !important;
              }
              div.st-key-reconnect_btn button[kind="primary"] {
                background: #f3f4f6 !important; /* gray-100 */
                background-color: #f3f4f6 !important;
                border-color: #e5e7eb !important; /* gray-200 */
                color: #6b7280 !important; /* gray-500 */
              }
              div.st-key-reconnect_btn button[kind="primary"]:hover {
                background: #ecfdf5 !important;
                background-color: #ecfdf5 !important;
                border-color: #34d399 !important;
                color: #047857 !important;
              }
              div.st-key-reconnect_btn button:active,
              div.st-key-reconnect_btn button:focus,
              div.st-key-reconnect_btn button[kind="primary"]:active,
              div.st-key-reconnect_btn button[kind="primary"]:focus,
              div.st-key-reconnect_btn button[kind="secondary"]:active,
              div.st-key-reconnect_btn button[kind="secondary"]:focus {
                box-shadow: 0 0 0 3px rgba(16,185,129,0.15) !important;
                outline: none !important;
              }
              /* Compact like/dislike buttons in chat */
              /* Targets Streamlit button wrappers whose keys contain chat_ and _like_/_dislike_ */
              /* Target the actual Streamlit button inside the keyed element container */
              div.element-container[class*="st-key-chat_"][class*="_like_"] .stButton > button,
              div.element-container[class*="st-key-chat_"][class*="_dislike_"] .stButton > button,
              div[class*="st-key-chat_"][class*="_like_"] .stButton > button,
              div[class*="st-key-chat_"][class*="_dislike_"] .stButton > button,
              div[class*="st-key-chat_"][class*="_like_"] button,
              div[class*="st-key-chat_"][class*="_dislike_"] button {
                border-radius: 9999px !important;
                padding: 0 !important;
                min-height: 20px !important;
                height: 20px !important;
                width: 20px !important;
                min-width: 20px !important;
                display: inline-flex !important;
                align-items: center !important;
                justify-content: center !important;
                line-height: 1 !important;
                font-size: 0 !important; /* hide emoji text */
                margin: 4px 4px 0 0 !important; /* top spacing to align with timestamp */
                box-shadow: none !important;
                background: transparent !important;
                background-color: transparent !important;
                border: none !important;
                border-color: transparent !important;
                color: inherit !important;
              }
              /* Inject white icons using CSS pseudo-elements */
              div.element-container[class*="st-key-chat_"][class*="_like_"] .stButton > button::before,
              div.element-container[class*="st-key-chat_"][class*="_dislike_"] .stButton > button::before,
              div[class*="st-key-chat_"][class*="_like_"] .stButton > button::before,
              div[class*="st-key-chat_"][class*="_dislike_"] .stButton > button::before,
              div[class*="st-key-chat_"][class*="_like_"] button::before,
              div[class*="st-key-chat_"][class*="_dislike_"] button::before {
                content: "";
                display: block !important;
                width: 16px !important;
                height: 16px !important;
                background-color: #6b7280 !important; /* gray-500 */
                -webkit-mask-repeat: no-repeat !important;
                -webkit-mask-position: center !important;
                -webkit-mask-size: contain !important;
                mask-repeat: no-repeat !important;
                mask-position: center !important;
                mask-size: contain !important;
              }
              /* Thumbs up mask */
              div.element-container[class*="st-key-chat_"][class*="_like_"] .stButton > button::before,
              div[class*="st-key-chat_"][class*="_like_"] .stButton > button::before,
              div[class*="st-key-chat_"][class*="_like_"] button::before {
                -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='black' d='M2 21h4V9H2v12zM22 10c0-1.1-.9-2-2-2h-5.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14 1 7.59 7.41C7.22 7.78 7 8.3 7 8.83V19c0 1.1.9 2 2 2h7c.82 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-1z'/></svg>") !important;
                mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='black' d='M2 21h4V9H2v12zM22 10c0-1.1-.9-2-2-2h-5.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14 1 7.59 7.41C7.22 7.78 7 8.3 7 8.83V19c0 1.1.9 2 2 2h7c.82 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-1z'/></svg>") !important;
              }
              /* Thumbs down mask */
              div.element-container[class*="st-key-chat_"][class*="_dislike_"] .stButton > button::before,
              div[class*="st-key-chat_"][class*="_dislike_"] .stButton > button::before,
              div[class*="st-key-chat_"][class*="_dislike_"] button::before {
                -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='black' d='M15 3H8c-.82 0-1.54.5-1.84 1.22L3.14 11.27c-.09.23-.14.47-.14.73v1c0 1.1.9 2 2 2h5.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L10 23l6.41-6.41c.37-.37.59-.89.59-1.42V5c0-1.1-.9-2-2-2zM22 3h-4v12h4V3z'/></svg>") !important;
                mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='black' d='M15 3H8c-.82 0-1.54.5-1.84 1.22L3.14 11.27c-.09.23-.14.47-.14.73v1c0 1.1.9 2 2 2h5.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L10 23l6.41-6.41c.37-.37.59-.89.59-1.42V5c0-1.1-.9-2-2-2zM22 3h-4v12h4V3z'/></svg>") !important;
              }
              /* Hover: darken icon color */
              div.element-container[class*="st-key-chat_"][class*="_like_"] .stButton > button:hover::before,
              div.element-container[class*="st-key-chat_"][class*="_dislike_"] .stButton > button:hover::before,
              div[class*="st-key-chat_"][class*="_like_"] .stButton > button:hover::before,
              div[class*="st-key-chat_"][class*="_dislike_"] .stButton > button:hover::before,
              div[class*="st-key-chat_"][class*="_like_"] button:hover::before,
              div[class*="st-key-chat_"][class*="_dislike_"] button:hover::before {
                background-color: #374151 !important; /* gray-700 */
              }
              /* Keep hover clean (no bg/border) */
              div[class*="st-key-chat_"][class*="_like_"] button:hover,
              div[class*="st-key-chat_"][class*="_dislike_"] button:hover,
              div[class*="st-key-chat_"][class*="_like_"] button:focus,
              div[class*="st-key-chat_"][class*="_dislike_"] button:focus,
              div[class*="st-key-chat_"][class*="_like_"] button:active,
              div[class*="st-key-chat_"][class*="_dislike_"] button:active {
                background: transparent !important;
                background-color: transparent !important;
                border: none !important;
                border-color: transparent !important;
                box-shadow: none !important;
                outline: none !important;
              }
              /* Timestamp smaller and muted; prevent wrapping */
              .chat-ts { font-size: 0.75rem; color: #6b7280; margin-top: 4px; white-space: nowrap; display: inline-block; }
            </style>
            """,
            unsafe_allow_html=True,
        )
        
        # Clean control layout - build a single row with inner columns for perfect alignment
        outer_left, _outer_right_spacer = st.columns([21, 1])

        with outer_left:
            # Label
            st.markdown("<div style='font-weight:600; font-size:0.9rem; margin-bottom:8px;'>Select Knowledgebase</div>", unsafe_allow_html=True)
            # Row containing select and button
            sel_col, btn_col = st.columns([20, 1])
            with sel_col:
                selected_name = st.selectbox(
                    "📚 Select Knowledgebase",
                    list(name_to_id.keys()),
                    key="kb_selector",
                    label_visibility="collapsed"
                )
            with btn_col:
                # Reconnect button (wrapped to avoid extra top spacing)
                with st.container(key="reconnect_btn_wrap"):
                    # Determine button label and type based on connection status
                    if "aarya_session_id" in st.session_state:
                        session_id = st.session_state.get("aarya_session_id", "")
                        button_label = "↻"
                        button_type = "primary"
                        button_help = f"Connected — Session: {session_id} — Click to start a new session"
                    else:
                        button_label = "↻"
                        button_type = "secondary"
                        button_help = "Not connected — Click to connect"

                    if st.button(button_label, type=button_type, help=button_help, key="reconnect_btn"):
                        if "aarya_session_id" in st.session_state:
                            del st.session_state["aarya_session_id"]
                        if "aarya_client_id" in st.session_state:
                            del st.session_state["aarya_client_id"]
                        st.toast("Session reset successfully", icon="✅")
                        st.rerun()

        selected_id = name_to_id[selected_name]
        # Toolbar: keep default Streamlit controls (e.g., theme toggle) with no overlays

        # Chat UI
        chat_key = f"chat_{selected_id}"
        if chat_key not in st.session_state["chat_histories"]:
            st.session_state["chat_histories"][chat_key] = []

        # Render history first (assistant messages right-aligned with reactions)
        for i, msg in enumerate(st.session_state["chat_histories"][chat_key]):
            role = msg.get("role")
            text = msg.get("content", "")
            ts = msg.get("ts") or ""
            msg_id_for_resp = msg.get("msg_id") or f"{chat_key}_msg_{i}"
            like = int(msg.get("like", 0))
            dislike = int(msg.get("dislike", 0))

            if role == "assistant":
                left, right = st.columns([5,7])
                with right:
                    st.markdown("<div class='msg-header right'><span class='name'>AARYA</span><span class='avatar avatar-assistant'>A</span></div>", unsafe_allow_html=True)
                    # Render markdown for assistant responses with custom styling
                    st.markdown(
                        f"""<div class="assistant-msg-wrapper">
                        
{text}

</div>""",
                        unsafe_allow_html=True
                    )
                    # Right-aligned meta row (timestamp + reactions)
                    meta = st.columns([9, 4])
                    # Like/Dislike controls for assistant messages
                    like_key = f"{chat_key}_like_{i}"
                    dislike_key = f"{chat_key}_dislike_{i}"
                    liked = bool(like)
                    disliked = bool(dislike)
                    has_reacted = liked or disliked
                    disable_like_btn = has_reacted
                    disable_dislike_btn = has_reacted
                    # Capture the user request that preceded this assistant response
                    prev_user_text = ""
                    try:
                        if i > 0:
                            prev_msg = st.session_state["chat_histories"][chat_key][i-1]
                            if (prev_msg or {}).get("role") == "user":
                                prev_user_text = str(prev_msg.get("content") or "")
                    except Exception:
                        prev_user_text = ""
                    # Active-state coloring for icons (green for like, red for dislike)
                    if liked:
                        st.markdown(
                            f"""
                            <style>
                              /* Force green icon for this specific like button (all states, including disabled) */
                              div[class*='st-key-{like_key}'] .stButton > button::before,
                              div[class*='st-key-{like_key}'] .stButton > button:hover::before,
                              div[class*='st-key-{like_key}'] .stButton > button:disabled::before,
                              div.element-container.st-key-{like_key} .stButton > button::before,
                              div.element-container.st-key-{like_key} .stButton > button:hover::before,
                              div.element-container.st-key-{like_key} .stButton > button:disabled::before {{
                                background-color: #059669 !important; /* emerald-600 */
                              }}
                            </style>
                            """,
                            unsafe_allow_html=True,
                        )
                    if disliked:
                        st.markdown(
                            f"""
                            <style>
                              /* Force red icon for this specific dislike button (all states, including disabled) */
                              div[class*='st-key-{dislike_key}'] .stButton > button::before,
                              div[class*='st-key-{dislike_key}'] .stButton > button:hover::before,
                              div[class*='st-key-{dislike_key}'] .stButton > button:disabled::before,
                              div.element-container.st-key-{dislike_key} .stButton > button::before,
                              div.element-container.st-key-{dislike_key} .stButton > button:hover::before,
                              div.element-container.st-key-{dislike_key} .stButton > button:disabled::before {{
                                background-color: #dc2626 !important; /* red-600 */
                              }}
                            </style>
                            """,
                            unsafe_allow_html=True,
                        )

                    with meta[1]:
                        # Order: Like, Dislike, Timestamp (timestamp right-aligned)
                        grp = st.columns([1, 1, 4])
                        with grp[0]:
                            if st.button(" ", key=like_key, help="Like", disabled=disable_like_btn):
                                # Send reaction over WebSocket (action=1)
                                try:
                                    ws_url = _get_ws_url()
                                    payload = {
                                        "action": "user_action",
                                        "sessionId": st.session_state.get("aarya_session_id", ""),
                                        "route": "general",
                                        "chatInput": prev_user_text,  # send the original user request
                                        "msg_id": msg_id_for_resp,
                                        "knowledge_id": selected_id,
                                        "knowledge_name": selected_name,
                                        "name": selected_name,
                                        "type": "message",
                                        "message": text,  # send response text
                                        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                                        "client_id": st.session_state.get("aarya_session_id", ""),
                                        "workflow_id": _get_workflow_id(),
                                        "action_code": "1",
                                        "reaction_for": msg_id_for_resp,
                                    }
                                    threading.Thread(target=_ws_send_message, args=(ws_url, payload, False), daemon=True).start()
                                except Exception:
                                    pass
                                # Toggle like; only one reaction at a time
                                cur = st.session_state["chat_histories"][chat_key][i]
                                if int(cur.get("like", 0)) == 1:
                                    cur["like"] = 0
                                    cur["dislike"] = 0
                                else:
                                    cur["like"] = 1
                                    cur["dislike"] = 0
                                st.rerun()
                        with grp[1]:
                            if st.button(" ", key=dislike_key, help="Dislike", disabled=disable_dislike_btn):
                                # Send reaction over WebSocket (action=2)
                                try:
                                    ws_url = _get_ws_url()
                                    payload = {
                                        "action": "user_action",
                                        "sessionId": st.session_state.get("aarya_session_id", ""),
                                        "route": "general",
                                        "chatInput": prev_user_text,
                                        "msg_id": msg_id_for_resp,
                                        "knowledge_id": selected_id,
                                        "knowledge_name": selected_name,
                                        "name": selected_name,
                                        "type": "message",
                                        "message": text,
                                        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                                        "client_id": st.session_state.get("aarya_session_id", ""),
                                        "workflow_id": _get_workflow_id(),
                                        "action_code": "2",
                                        "reaction_for": msg_id_for_resp,
                                    }
                                    threading.Thread(target=_ws_send_message, args=(ws_url, payload, False), daemon=True).start()
                                except Exception:
                                    pass
                                cur = st.session_state["chat_histories"][chat_key][i]
                                if int(cur.get("dislike", 0)) == 1:
                                    cur["like"] = 0
                                    cur["dislike"] = 0
                                else:
                                    cur["like"] = 0
                                    cur["dislike"] = 1
                                st.rerun()
                        # Timestamp, right-aligned inside the group
                        grp[2].markdown(f"<div style='text-align:right' class='chat-ts'>{ts}</div>", unsafe_allow_html=True)
            else:
                # Escape HTML/XML characters for user messages
                text_escaped = html.escape(text).replace('\n', '<br>')
                left, right = st.columns([8,4])
                with left:
                    st.markdown("<div class='msg-header'><span class='avatar avatar-user'>Y</span><span class='name'>You</span></div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='bubble-user'>{text_escaped}</div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='meta-row'>{ts}</div>", unsafe_allow_html=True)

        # Modern chat input
        user_msg = st.chat_input("Ask about the selected Knowledgebase...")
        if user_msg:
            # Immediately add user message to chat history
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.session_state["chat_histories"][chat_key].append({
                "role": "user",
                "content": user_msg,
                "ts": now,
                "like": 0,
                "dislike": 0,
            })
            
            # Add placeholder for assistant response with typing indicator
            assistant_index = len(st.session_state["chat_histories"][chat_key])
            resp_msg_id = f"msg_id-{int(time.time()*1000)}"
            st.session_state["chat_histories"][chat_key].append({
                "role": "assistant",
                "content": "💬 Typing...",
                "ts": now,
                "msg_id": resp_msg_id,
                "like": 0,
                "dislike": 0,
            })
            
            # Send WebSocket request in background
            ws_url = _get_ws_url()
            if ws_url:
                if "aarya_session_id" not in st.session_state:
                    st.session_state["aarya_session_id"] = f"session_{uuid.uuid4().hex[:8]}_{int(time.time()*1000)}"
                if "aarya_client_id" not in st.session_state:
                    st.session_state["aarya_client_id"] = f"selfcare_{int(time.time()*1000)}_{str(uuid.uuid4().int)[-3:]}"
                ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                wid = _get_workflow_id()
                msg_id = resp_msg_id
                payload = {
                    "action": "sendMessage",
                    "sessionId": st.session_state["aarya_session_id"],
                    "route": "general",
                    "chatInput": user_msg,
                    "msg_id": msg_id,
                    "knowledge_id": selected_id,
                    "knowledge_name": selected_name,
                    "name": selected_name,
                    "type": "message",
                    "message": user_msg,
                    "timestamp": ts,
                    "client_id": st.session_state["aarya_client_id"],
                    "workflow_id": wid,
                }
                
                # Store pending request
                st.session_state["pending_ws"] = {
                    "ws_url": ws_url,
                    "payload": payload,
                    "chat_key": chat_key,
                    "assistant_index": assistant_index
                }
            
            # Rerun to show user message and typing indicator
            st.rerun()
        
        # Process pending WebSocket request
        if "pending_ws" in st.session_state:
            pending = st.session_state["pending_ws"]
            del st.session_state["pending_ws"]
            
            # Call WebSocket
            answer = _ws_send_message(pending["ws_url"], pending["payload"], debug=False)
            
            if answer:
                # Update typing indicator with actual response
                st.session_state["chat_histories"][pending["chat_key"]][pending["assistant_index"]]["content"] = answer
            else:
                # Remove the typing indicator if no response
                st.session_state["chat_histories"][pending["chat_key"]].pop(pending["assistant_index"])
            
            # Rerun to show final response
            st.rerun()