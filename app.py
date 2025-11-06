import os
import warnings
import requests
import urllib.parse
import io
import base64
try:
    import tomllib as _toml  # Python 3.11+
except Exception:  # pragma: no cover
    _toml = None
import uuid
import json
from datetime import datetime
warnings.filterwarnings("ignore", message=r".*st\.cache` is deprecated.*", category=DeprecationWarning)
import streamlit as st
from streamlit_option_menu import option_menu
import pandas as pd
from typing import List, Dict, Optional
try:
    from streamlit_cookies_manager import EncryptedCookieManager  # type: ignore
except Exception:
    EncryptedCookieManager = None  # type: ignore

from lib.storage import ProductStore
from lib.pdf_utils import extract_text_from_pdf, chunk_text
from lib.retriever import Retriever
# OAuth component will be lazy-imported inside the auth function to avoid NameError interruptions

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")
TEXT_DIR = os.path.join(DATA_DIR, "texts")

os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(TEXT_DIR, exist_ok=True)

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
    st.session_state["nav_page"] = "aarya"
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
        for k, v in qp.items():
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
    # Clear session
    st.session_state["user"] = None
    st.session_state["google_access_token"] = None
    st.session_state["show_profile"] = False
    # Set a flash query param so we can show an alert after rerun on the login screen
    try:
        st.experimental_set_query_params(logged_out="1")
    except Exception:
        pass
    # Clear user cookie
    try:
        if cookies is not None:
            try:
                del cookies["user"]
            except Exception:
                cookies["user"] = ""
            cookies.save()
    except Exception:
        pass
    st.rerun()

def _handle_logout_param():
    params = _read_query_params()
    has_logout = "logout" in params and ("1" in params.get("logout", []))
    if has_logout:
        _logout()

_handle_logout_param()

# Restore user from cookie if session empty, but do NOT restore right after logout
if not st.session_state.get("user") and cookies is not None:
    _qp_restore = {}
    try:
        _qp_restore = _read_query_params()
    except Exception:
        _qp_restore = {}
    if "logged_out" not in _qp_restore:
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
st.set_page_config(page_title="Product Assistant", page_icon=page_icon, layout="wide", initial_sidebar_state="expanded")

# Prefer native logo placement (goes into stLogoSpacer) — use logo.svg for navbar
LOGO1_SVG_PATH = os.path.join(ASSETS_DIR, "logo1.svg")
_chosen_logo_rel = "assets/logo.svg" if os.path.exists(LOGO_SVG_PATH) else ("assets/logo1.svg" if os.path.exists(LOGO1_SVG_PATH) else None)
try:
    if _chosen_logo_rel:
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
      div[data-testid='stMainMenu'], #MainMenu { display: none !important; visibility: hidden !important; }
      /* Put title into Streamlit's top toolbar and allow right-side chip */
      div[data-testid="stToolbar"] { position: relative; overflow: visible; padding-right: 180px; }
      /* Prevent Streamlit toolbar actions from intercepting clicks over our chip */
      div[data-testid="stToolbarActions"] { pointer-events: none !important; }
      .tb-chip, .tb-profile { pointer-events: auto; }
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
        content: "Product Assistant";
        position: absolute;
        left: 40px; /* space for toolbar icon */
        top: 50%;
        transform: translateY(-50%);
        font-weight: 600;
        font-size: 14px;
        color: #111827;
        letter-spacing: 0.2px;
        pointer-events: none;
        max-width: calc(100% - 220px); /* avoid overlap with right-side toolbar icons */
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
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
      }
      /* Sidebar radio spacing */
      section[data-testid="stSidebar"] label { margin-bottom: 4px; }

      /* Sidebar look & option-menu polish */
      section[data-testid="stSidebar"] {
        background: var(--sidebar-bg);
      }
      /* Reduce default Streamlit sidebar inner padding */
      section[data-testid="stSidebar"] .block-container { padding-top: 4px; padding-bottom: 10px; }
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
      .bubble-assistant { background:#f1f5ff; border:1px solid #dbe4ff; border-radius: 12px; padding:10px 12px; margin-bottom:6px; }
      .meta-row { font-size:12px; color:#6b7280; margin-top:2px; }
      .msg-header { display:flex; align-items:center; gap:8px; font-weight:600; margin: 2px 0 6px 0; }
      .msg-header.right { justify-content: flex-end; }
      .avatar { width:24px; height:24px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:14px; }
      .avatar-user { background:#e5e7eb; color:#374151; }
      .avatar-assistant { background:#dbe4ff; color:#1d4ed8; }
      .name { font-size:13px; color:#111827; }
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
                result = oauth2.authorize_button("sign in with sixdee mail", redir, scope="openid email profile", key="google")
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
          .login-logo { width:68px; height:68px; border-radius:50%; background:#f97316; display:flex; align-items:center; justify-content:center; margin: 8px auto 12px; box-shadow:0 6px 16px rgba(249,115,22,0.35); }
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

    # Optional logout flash
    try:
        _qp = _read_query_params()
        if "logged_out" in _qp and ("1" in _qp.get("logged_out", [])):
            st.success("You have been logged out.")
            try:
                st.experimental_set_query_params()
            except Exception:
                pass
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
        name = st.text_input("Product name", key=f"name_{prefix}")
        desc = st.text_area("Product description", key=f"desc_{prefix}")
        pdf_file = st.file_uploader("Upload product PDF", type=["pdf"], key=f"pdf_{prefix}")
        submitted = st.form_submit_button("Create/Update Product")
    if submitted:
        if not name:
            st.warning("Please enter a product name.")
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
            store.upsert({
                "id": product_id,
                "name": name,
                "description": desc or "",
                "pdf_path": pdf_path,
            })
            retriever.index_product(product_id, chunks)
            st.success("Product saved and indexed successfully.")

# Create Product dialog (admin protected) with fallback
if st.session_state.get("show_create_dialog"):
    if hasattr(st, "dialog"):
        with st.dialog("Create Product"):
            _render_create_form("dialog")
            cols = st.columns([1, 1, 1])
            if cols[2].button("Close"):
                st.session_state["show_create_dialog"] = False
    else:
        with st.sidebar:
            st.subheader("Create Product")
            _render_create_form("sidebar")
            if st.button("Close"):
                st.session_state["show_create_dialog"] = False

# Footer logo removed per request

# Ensure a safe default for the current page before rendering sidebar/menu
page = st.session_state.get("nav_page", "aarya")

# Minimal sidebar navigation (clean, no captions)
with st.sidebar:
    _options = ["aarya"]
    if is_admin_user():
        _options.append("Knowledge base")
    _default_index = 0 if page == "aarya" else 1
    try:
        side_selected = option_menu(
            menu_title=None,
            options=_options,
            icons=["chat-dots", "folder-plus"],
            menu_icon="list",
            default_index=_default_index,
            styles={
                "container": {"padding": "0", "background-color": "transparent", "margin": "0"},
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
    page = st.session_state.get("nav_page", "aarya")

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
          /* No extra visible logout button; handled via JS click on the text */
        </style>
        """,
        unsafe_allow_html=True,
    )

    avatar_small_html = f"<img src='{pic}' alt='avatar'/>" if pic else initial
    avatar_large_html = f"<img src='{pic}' alt='avatar'/>" if pic else initial

    chip_html = (
        "<div class='tb-chip'>"
        "  <div class='chip'>"
        f"      <div class='avatar' title='{u.get('email','')}'>{avatar_small_html}</div>"
        f"      <div class='name'><span>{name_html}</span></div>"
        "  </div>"
        "</div>"
    )

    # Dropdown with a direct anchor link that navigates to ?logout=1
    dropdown_html = (
        "<div class='tb-profile'>"
        "  <div class='row'>"
        f"    <div class='avatar-xl'>{avatar_large_html}</div>"
        "    <div style='min-width:0'>"
        f"      <div class='name'>{name_html}</div>"
        f"      <div class='email'>{u.get('email','')}</div>"
        "    </div>"
        "    <a class='logout-btn' href='./?logout=1' role='button'>Logout</a>"
        "  </div>"
        "</div>"
    ) if prof_on else ""

    # Render chip and dropdown
    st.markdown(chip_html + dropdown_html, unsafe_allow_html=True)
    # Only render the overlay toggle button when dropdown is closed to avoid intercepting clicks
    if not prof_on:
        if st.button(" ", key="chip_toggle_btn"):
            st.session_state["show_profile"] = True
            st.rerun()
    # No extra visible logout controls; link navigates with ?logout=1 which triggers server _logout()

# ---------------------- Knowledge Base Page ----------------------
if page == "Knowledge base":
    # Update toolbar title for this page
    st.markdown(
        """
        <style>
          div[data-testid='stToolbar']::after { content: 'Knowledge base'; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if not is_admin_user():
        st.info("This page is restricted to admins.")
    else:
        # Manage knowledge base (Edit/Delete only)
        products = store.list()
        # Create/Edit appears before the table; driven by previously selected rows (from session_state)
        sel_rows_state = st.session_state.get("kb_selected_rows", [])
        is_edit_state = len(sel_rows_state) == 1
        st.markdown(f"**{'Edit knowledge base' if is_edit_state else 'Create knowledge base'}**")
        with st.container():
            if is_edit_state:
                _pid = sel_rows_state[0].get("id")
                _cur = next((p for p in products if p.get("id") == _pid), None) or {}
                name_val = st.text_input("Name", value=_cur.get("name", ""), key=f"kb_ce_name_{_pid}")
                desc_val = st.text_area("Description", value=_cur.get("description", ""), key=f"kb_ce_desc_{_pid}")
                new_pdf = st.file_uploader("Replace PDF (optional)", type=["pdf"], key=f"kb_ce_edit_pdf_{_pid}")
                cur_fname = os.path.basename(_cur.get("pdf_path", "")) if _cur.get("pdf_path") else "-"
                st.caption(f"Current file: {cur_fname}")
                if new_pdf is not None:
                    try:
                        st.caption(f"Selected new file: {getattr(new_pdf, 'name', 'uploaded.pdf')}")
                    except Exception:
                        pass
                _c_by = _cur.get("created_by") or "-"
                _c_at = _cur.get("created_at") or "-"
                _u_by = _cur.get("updated_by") or "-"
                _u_at = _cur.get("updated_at") or "-"
                st.markdown(
                    f"<div style='font-size:12px; color:#666;'>Created by <b>{_c_by}</b> on <b>{_c_at}</b> • Last updated by <b>{_u_by}</b> on <b>{_u_at}</b></div>",
                    unsafe_allow_html=True,
                )
                if st.button("Save changes", key=f"kb_ce_save_{_pid}"):
                    if (
                        name_val != _cur.get("name") or
                        desc_val != _cur.get("description") or
                        new_pdf is not None
                    ):
                        user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                        # If a new PDF was uploaded, overwrite and re-index
                        pdf_path = _cur.get("pdf_path", "")
                        if new_pdf is not None and pdf_path:
                            try:
                                with open(pdf_path, "wb") as f:
                                    f.write(new_pdf.read())
                                try:
                                    text = extract_text_from_pdf(pdf_path)
                                    chunks = chunk_text(text)
                                except Exception:
                                    chunks = []
                                try:
                                    retriever.index_product(_pid, chunks)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        store.upsert({
                            "id": _pid,
                            "name": name_val,
                            "description": desc_val,
                            "pdf_path": pdf_path,
                            "emails": _cur.get("emails", []),
                            # preserve creation metadata; update modification metadata
                            "created_by": _cur.get("created_by"),
                            "created_at": _cur.get("created_at"),
                            "updated_by": user_email,
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        })
                        st.success("Saved changes.")
                        st.rerun()
                    else:
                        st.info("No changes detected.")
            else:
                name_c = st.text_input("Product name", key="kb_ce_new_name")
                desc_c = st.text_input("Short description", key="kb_ce_new_desc")
                pdf_c = st.file_uploader("Upload product PDF", type=["pdf"], key="kb_ce_new_pdf")
                if st.button("Create Product", key="kb_ce_new_submit"):
                    if not name_c:
                        st.warning("Please enter a product name.")
                    elif not pdf_c:
                        st.warning("Please upload a product PDF.")
                    else:
                        pid = str(uuid.uuid4())
                        pdf_path = os.path.join(PDF_DIR, f"{pid}.pdf")
                        with open(pdf_path, "wb") as f:
                            f.write(pdf_c.read())
                        try:
                            text = extract_text_from_pdf(pdf_path)
                            chunks = chunk_text(text)
                        except Exception as e:
                            st.error(f"Failed to process PDF: {e}")
                            chunks = []
                        user_email = ((st.session_state.get("user") or {}).get("email") or "").strip().lower()
                        emails_list: List[str] = [user_email] if user_email else []
                        store.upsert({
                            "id": pid,
                            "name": name_c,
                            "description": desc_c or "",
                            "pdf_path": pdf_path,
                            "emails": emails_list,
                            # creation metadata; not editable via UI
                            "created_by": user_email,
                            "created_at": datetime.now().isoformat(timespec="seconds"),
                            "updated_by": "",
                            "updated_at": "",
                        })
                        retriever.index_product(pid, chunks)
                        st.success("Product created and indexed successfully.")
                        st.rerun()

        # Native editable data table with search + pagination
        if products:
            st.markdown('<div class="app-card">', unsafe_allow_html=True)
            st.markdown('<div class="app-section-title">Existing knowledges</div>', unsafe_allow_html=True)
            # Toolbar + CSS
            st.markdown(
                """
                <style>
                  .kb-toolbar { display:flex; gap:10px; align-items:center; justify-content:space-between; margin-bottom:8px; }
                  .kb-right { display:flex; gap:10px; align-items:center; }
                  /* Constrain controls inside toolbar */
                  .kb-toolbar .stSelectbox, .kb-toolbar .stTextInput { max-width: 100px; }
                  .kb-toolbar .stTextInput input { max-width: 100px; }
                  /* Make action icon buttons borderless in rows */
                  .kb-rows .stButton>button {
                    border: 0 !important;
                    outline: none !important;
                    background: transparent !important;
                    box-shadow: none !important;
                    padding: 0 6px !important;
                    min-height: auto !important;
                    line-height: 1 !important;
                    cursor: pointer;
                  }
                  .kb-rows .stButton>button:hover {
                    background: rgba(0,0,0,0.05) !important;
                  }
                  .kb-rows .stButton>button:focus,
                  .kb-rows .stButton>button:focus-visible,
                  .kb-rows .stButton>button:active {
                    outline: none !important;
                    box-shadow: none !important;
                    background: transparent !important;
                  }
                  /* Tighten spacing between Edit (expander) and table */
                  div[data-testid="stExpander"] { margin-bottom: 6px; }
                  .kb-rows { margin-top: 0; }
                  .kb-rows .kb-sep { height: 1px; background: #e5e7eb; margin: 6px 0; }
                </style>
                """,
                unsafe_allow_html=True,
            )
            st.markdown('<div class="kb-toolbar">', unsafe_allow_html=True)
            col_left, col_mid, col_right = st.columns([0.18, 0.64, 0.18])
            with col_left:
                ps = st.selectbox(
                    "Rows per page",
                    [5, 10, 20],
                    index=0,
                    key="kb_ps_ed",
                    label_visibility="visible",
                )
            with col_right:
                q = st.text_input(
                    "Search",
                    key="kb_search",
                    placeholder="Name, description, emails",
                    label_visibility="visible",
                )
            st.markdown('</div>', unsafe_allow_html=True)
            filtered = products
            if q:
                ql = q.strip().lower()
                filtered = []
                for p in products:
                    name = (p.get("name") or "").lower()
                    desc = (p.get("description") or "").lower()
                    ems = ", ".join(p.get("emails", [])) .lower()
                    if (ql in name) or (ql in desc) or (ql in ems):
                        filtered.append(p)
            # Pagination (after filtering)
            if "kb_pg_ed" not in st.session_state: st.session_state["kb_pg_ed"] = 1
            total = len(filtered)
            pages = max((total + ps - 1) // ps, 1)
            page = min(max(st.session_state.get("kb_pg_ed", 1), 1), pages)
            start = (page - 1) * ps
            end = min(start + ps, total)
            page_items = filtered[start:end]

            # Render read-only rows with in-row action icons
            if not page_items:
                st.markdown('<div class="app-muted">No products on this page.</div>', unsafe_allow_html=True)
            else:
                # Header
                st.markdown('<div class="kb-rows">', unsafe_allow_html=True)
                h1,h2,h3,h4,h5,h6,h7 = st.columns([2,3,2,2,2,2,1])
                h1.markdown("**Name**")
                h2.markdown("**Description**")
                h3.markdown("**Created by**")
                h4.markdown("**Created at**")
                h5.markdown("**Updated by**")
                h6.markdown("**Updated at**")
                h7.markdown("**Actions**")
                # Rows
                for i, item in enumerate(page_items):
                    pid = item.get("id")
                    name = item.get("name","")
                    desc = item.get("description","")
                    cby = item.get("created_by","")
                    cat = item.get("created_at","")
                    uby = item.get("updated_by","")
                    uat = item.get("updated_at","")
                    cols = st.columns([2,3,2,2,2,2,1])
                    cols[0].write(name)
                    cols[1].write(desc)
                    cols[2].write(cby)
                    cols[3].write(cat)
                    cols[4].write(uby)
                    cols[5].write(uat)
                    with cols[6]:
                        c_a, c_b = st.columns([1,1])
                        if c_a.button("✏️", key=f"kb_icon_edit_{pid}"):
                            st.session_state["kb_selected_rows"] = [{"id": pid}]
                            st.rerun()
                        if c_b.button("🗑️", key=f"kb_icon_del_{pid}"):
                            store.delete(pid)
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
                            st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

                nav_l, nav_c, nav_r = st.columns([1,2,1])
                with nav_l:
                    if st.button("<", disabled=(page<=1), key="kb_prev2"):
                        st.session_state["kb_pg_ed"] = max(page-1, 1)
                        st.rerun()
                with nav_c:
                    st.markdown(f"<div style='text-align:center; font-weight:500;'>Page {page} / {pages}</div>", unsafe_allow_html=True)
                with nav_r:
                    if st.button(">", disabled=(page>=pages), key="kb_next2"):
                        st.session_state["kb_pg_ed"] = min(page+1, pages)
                        st.rerun()

                st.caption(f"Showing {end-start if total>0 else 0} of {total} entries. Page {page}/{pages}.")
            st.markdown('</div>', unsafe_allow_html=True)

# ---------------------- aarya Page ----------------------
elif page == "aarya":

    products = store.list()
    if not products:
        st.info("No products available. An admin must create one first.")
    else:
        name_to_id = {p["name"]: p["id"] for p in products}
        selected_name = st.selectbox("Select your knowledge base", list(name_to_id.keys()))
        selected_id = name_to_id[selected_name]
        # Update toolbar title to current product name with professional suffix
        safe_title = selected_name.replace("'", "\\'") + " — Product QA"
        st.markdown(
            f"""
            <style>
              div[data-testid='stToolbar']::after {{ content: '{safe_title}'; }}
            </style>
            """,
            unsafe_allow_html=True,
        )

        # Chat UI
        chat_key = f"chat_{selected_id}"
        if chat_key not in st.session_state["chat_histories"]:
            st.session_state["chat_histories"][chat_key] = []

        # Render history first (assistant messages right-aligned with reactions)
        for i, msg in enumerate(st.session_state["chat_histories"][chat_key]):
            role = msg.get("role")
            text = msg.get("content", "")
            ts = msg.get("ts") or ""
            like = int(msg.get("like", 0))
            dislike = int(msg.get("dislike", 0))

            if role == "assistant":
                left, right = st.columns([5,7])
                with right:
                    st.markdown("<div class='msg-header right'><span class='name'>AARYA</span><span class='avatar avatar-assistant'>A</span></div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='bubble-assistant'>{text}</div>", unsafe_allow_html=True)
                    meta = st.columns([9,3])
                    meta[1].markdown(f"<div class='chat-ts'>{ts}</div>", unsafe_allow_html=True)
            else:
                left, right = st.columns([8,4])
                with left:
                    st.markdown("<div class='msg-header'><span class='avatar avatar-user'>Y</span><span class='name'>You</span></div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='bubble-user'>{text}</div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='meta-row'>{ts}</div>", unsafe_allow_html=True)

        # Modern chat input
        user_msg = st.chat_input("Ask about the selected product...")
        if user_msg:
            try:
                top = retriever.query(selected_id, user_msg, top_k=3)
            except Exception as e:
                top = []
                st.error(f"Retrieval error: {e}")

            context = "\n\n".join([c["text"] for c in top]) if top else ""
            if context:
                answer = (
                    "Here are the most relevant excerpts from the product document:\n\n"
                    + context
                )
            else:
                answer = "Sorry, I couldn't find relevant information in the product PDF."

            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.session_state["chat_histories"][chat_key].append({
                "role": "user",
                "content": user_msg,
                "ts": now,
                "like": 0,
                "dislike": 0,
            })
            st.session_state["chat_histories"][chat_key].append({
                "role": "assistant",
                "content": answer,
                "ts": now,
                "like": 0,
                "dislike": 0,
            })
            # Rely on rerun to render new messages with reactions/timestamps
