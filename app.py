import os
import warnings
import requests
import urllib.parse
import io
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
    return st.secrets.get("ADMIN_PASSWORD", "admin")


def is_admin_authenticated() -> bool:
    return st.session_state.get("is_admin", False)


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
    # Clear query params
    try:
        st.experimental_set_query_params()
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

# Restore user from cookie if session empty
if not st.session_state.get("user") and cookies is not None:
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

# Profile dropdown visibility is controlled by session flag only (no URL navigation)

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
                result = oauth2.authorize_button("Continue with Google", redir, scope="openid email profile", key="google")
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
                st.link_button("Login with Google (fallback)", f"{auth_url}?{qs}")
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
                st.session_state["user"] = None
                st.rerun()

# -------- Auth gate: require Google login before showing the app --------
if not st.session_state.get("user"):
    # Minimal login: hide sidebar and center only the Google button
    st.markdown(
        """
        <style>
          [data-testid="stSidebar"] { display: none !important; }
          div[data-testid='stToolbar']::before, div[data-testid='stToolbar']::after { content: none !important; }
          [data-testid="stAppViewContainer"] > .main { display:flex; align-items:center; justify-content:center; min-height: 100vh; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _render_auth()
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
    if not is_admin_authenticated():
        with st.form(f"admin_login_{prefix}"):
            pwd = st.text_input("Admin password", type="password")
            login = st.form_submit_button("Login")
        if login:
            if pwd == get_admin_password():
                st.session_state["is_admin"] = True
                st.success("Logged in as admin")
            else:
                st.error("Invalid password")
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
    _options = ["aarya", "Knowledge base"]
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
    except Exception:
        side_selected = st.radio("", _options, index=_default_index, key="sidebar_menu_fallback")
    if side_selected != page:
        page = side_selected
        st.session_state["nav_page"] = page



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
          .tb-profile { position: fixed; top: 44px; right: 16px; z-index: 2147483647; background:#fff; border:1px solid #e5e7eb; border-radius:12px; box-shadow:0 8px 24px rgba(0,0,0,0.12); min-width: 260px; max-width: 320px; overflow:hidden; }
          .tb-profile .row { display:flex; align-items:center; gap:12px; padding: 12px 14px; }
          .tb-profile .row + .row { border-top: 1px solid #f1f5f9; }
          .tb-profile .avatar-xl { width:40px; height:40px; border-radius:50%; background:#e5e7eb; color:#374151; display:flex; align-items:center; justify-content:center; font-size:16px; overflow:hidden; }
          .tb-profile .avatar-xl img { width:100%; height:100%; object-fit: cover; display:block; }
          .tb-profile .name { font-weight:600; color:#111827; }
          .tb-profile .email { font-size:12px; color:#6b7280; }
          .tb-profile .logout-btn { margin-left:auto; color:#ef4444; text-decoration:none; font-size:13px; }
          .tb-profile .logout-btn:hover { text-decoration: underline; }
          /* Transparent button overlay positioned over the chip to catch clicks */
          div.st-key-chip_toggle_btn { position: fixed; top: 8px; right: 16px; width: 200px; height: 36px; z-index: 2147483650; }
          div.st-key-chip_toggle_btn button { width: 100%; height: 100%; background: transparent !important; border: 0 !important; color: transparent !important; box-shadow: none !important; }
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

    dropdown_html = (
        "<div class='tb-profile'>"
        "  <div class='row'>"
        f"    <div class='avatar-xl'>{avatar_large_html}</div>"
        "    <div style='min-width:0'>"
        f"      <div class='name'>{name_html}</div>"
        f"      <div class='email'>{u.get('email','')}</div>"
        "    </div>"
        "    <span class='logout-btn' style='cursor:default;opacity:0.7'>Logout</span>"
        "  </div>"
        "</div>"
    ) if prof_on else ""

    # Render chip and dropdown
    st.markdown(chip_html + dropdown_html, unsafe_allow_html=True)
    # Overlay a transparent Streamlit button to toggle the dropdown via session
    if st.button(" ", key="chip_toggle_btn"):
        st.session_state["show_profile"] = not st.session_state.get("show_profile", False)
        st.rerun()
    # Real Logout button (works even if the link is blocked); shown only when dropdown is open
    if prof_on:
        # Small spacer to avoid overlap
        st.write("")
        if st.button("Logout", key="logout_action_btn"):
            _logout()

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

    if not is_admin_authenticated():
        st.info("This page is restricted to admins. Please login.")
        admin_login_form()
    else:
        st.markdown('<div class="app-card">', unsafe_allow_html=True)
        st.markdown('<div class="app-section-title">Create product</div>', unsafe_allow_html=True)
        with st.form("product_form"):
            cols = st.columns([1,1])
            with cols[0]:
                name = st.text_input("Product name")
            with cols[1]:
                desc = st.text_input("Short description")
            pdf_file = st.file_uploader("Upload product PDF", type=["pdf"]) 
            submitted = st.form_submit_button("Create/Update Product")
        st.markdown('</div>', unsafe_allow_html=True)

        if submitted:
            if not name:
                st.warning("Please enter a product name.")
            elif not pdf_file:
                st.warning("Please upload a product PDF.")
            else:
                # Save/Update product
                product = store.get_by_name(name)
                if product is None:
                    product_id = str(uuid.uuid4())
                else:
                    product_id = product["id"]

                # Persist PDF
                pdf_path = os.path.join(PDF_DIR, f"{product_id}.pdf")
                with open(pdf_path, "wb") as f:
                    f.write(pdf_file.read())

                # Extract & chunk text
                try:
                    text = extract_text_from_pdf(pdf_path)
                    chunks = chunk_text(text)
                except Exception as e:
                    st.error(f"Failed to process PDF: {e}")
                    chunks = []

                # Persist product metadata
                store.upsert({
                    "id": product_id,
                    "name": name,
                    "description": desc or "",
                    "pdf_path": pdf_path,
                })

                # Index text for retrieval
                retriever.index_product(product_id, chunks)

                st.success("Product saved and indexed successfully.")

        # Product list
        products = store.list()
        st.markdown('<div class="app-card">', unsafe_allow_html=True)
        st.markdown('<div class="app-section-title">Existing products</div>', unsafe_allow_html=True)
        if products:
            df = pd.DataFrame(products)[["name", "description", "id"]]
            st.dataframe(df, width='stretch', hide_index=True)
        else:
            st.markdown('<div class="app-muted">No products yet.</div>', unsafe_allow_html=True)
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
