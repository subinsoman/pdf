import os
import html
import streamlit as st
import pandas as pd
from lib.db_config import DatabaseConfig
from lib.pdf_metadata_repo import PdfMetadataRepository
from lib.clients_metadata_repo import ClientsMetadataRepository


class Dashboard:
    def __init__(self) -> None:
        # lazy-init repo to avoid import issues on module load
        self._repo = None
        self._clients_repo = None

    def _get_repo(self) -> PdfMetadataRepository:
        if self._repo is None:
            db = DatabaseConfig(base_dir=os.path.dirname(__file__))
            engine = db.create_engine()
            self._repo = PdfMetadataRepository(engine)
        return self._repo

    def _get_clients_repo(self) -> ClientsMetadataRepository:
        if self._clients_repo is None:
            db = DatabaseConfig(base_dir=os.path.dirname(__file__))
            engine = db.create_engine()
            self._clients_repo = ClientsMetadataRepository(engine)
        return self._clients_repo

    def _fetch_counts(self):
        @st.cache_data(ttl=15, show_spinner=False)
        def _load_counts():
            try:
                rows_local = self._get_repo().list_all()
            except Exception:
                rows_local = []
            return rows_local

        rows = _load_counts()

        def norm_status(v):
            if v is None:
                return ""
            s = str(v).strip()
            # Map common numeric codes used elsewhere
            if s == "0":
                return "New"
            if s == "1":
                return "Success"
            if s == "2":
                return "In Progress"
            if s == "3":
                return "Deleted"
            return s

        statuses = [norm_status(r.get("status")) for r in rows]
        active_rows = [r for r, s in zip(rows, statuses) if s.lower() != "deleted"]
        success = sum(1 for s in statuses if s.lower() == "success")
        in_progress = sum(1 for s in statuses if s.lower() in ("in progress", "in_progress", "processing"))
        deleted = sum(1 for s in statuses if s.lower() == "deleted")

        return {
            "total": len(rows),
            "active_total": len(active_rows),
            "success": success,
            "in_progress": in_progress,
            "deleted": deleted,
        }

    def _fetch_conversation_stats(self):
        @st.cache_data(ttl=15, show_spinner=False)
        def _load_conv():
            try:
                total = self._get_clients_repo().count_distinct_sessions()
            except Exception:
                total = 0
            try:
                recent_local = self._get_clients_repo().list_recent(25)
            except Exception:
                recent_local = []
            return total, recent_local

        total_conversations, recent = _load_conv()

        # Only keep selected columns for display
        cols = [
            "time",
            "client_name",
            "client_email",
            "sessionId",
            "msg_count",
            "knowledge_id",
        ]
        recent_view = [
            {k: r.get(k) for k in cols}
            for r in recent
        ]
        return total_conversations, recent_view

    def _fetch_client_aggregates(self):
        """Return totals from clients metadata and resolve most used knowledge name if possible."""
        @st.cache_data(ttl=15, show_spinner=False)
        def _load_aggs():
            total_requests_l = 0
            unique_users_l = 0
            top_id_l = None
            top_requests_l = 0
            try:
                total_requests_l = self._get_clients_repo().total_requests()
            except Exception:
                pass
            try:
                unique_users_l = self._get_clients_repo().count_unique_users()
            except Exception:
                pass
            try:
                top = self._get_clients_repo().most_used_knowledge() or {}
                top_id_l = top.get("knowledge_id")
                _rows = int(top.get("rows_count") or 0)
                _msgs = int(top.get("msgs_sum") or 0)
                top_requests_l = _rows if _rows > 0 else _msgs
            except Exception:
                top_id_l = None
                top_requests_l = 0
            return total_requests_l, unique_users_l, top_id_l, top_requests_l

        total_requests, unique_users, top_id, top_requests = _load_aggs()

        # Resolve Knowledgebase name from pdf_metadata by matching id as string
        top_name = None
        if top_id is not None:
            @st.cache_data(ttl=15, show_spinner=False)
            def _resolve_name(_sid: str):
                try:
                    all_rows_local = self._get_repo().list_all()
                    match_local = next((r for r in all_rows_local if str(r.get("id")) == _sid), None)
                    if match_local and match_local.get("name"):
                        return str(match_local.get("name"))
                except Exception:
                    return None
                return None
            sid = str(top_id)
            top_name = _resolve_name(sid)

        return {
            "total_requests": total_requests,
            "unique_users": unique_users,
            "most_used_id": top_id,
            "most_used_name": top_name,
            "most_used_requests": top_requests,
        }

    def render(self) -> None:
        # Global polish for sections and cards
        st.markdown(
            """
            <style>
              .db-section-title { font-size: 1.1rem; font-weight: 600; margin: 4px 0 10px 0; }
              .kpi-card { background:#ffffff; border:1px solid #e5e7eb; border-radius:12px; padding:14px 16px; box-shadow:0 1px 2px rgba(0,0,0,0.03); }
              .kpi-title { color:#6b7280; font-size:12px; font-weight:600; letter-spacing:.2px; text-transform:uppercase; }
              .kpi-row { display:flex; align-items:center; justify-content:space-between; margin-top:4px; }
              .kpi-value { font-size:22px; font-weight:700; color:#111827; }
              .kpi-sub { font-size:12px; color:#6b7280; }
              .kpi-icon { font-size:18px; opacity:.9; }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # Metrics from database (formatted)
        counts = self._fetch_counts()
        t_total = f"{counts.get('total', 0):,}"
        t_active = f"{counts.get('active_total', 0):,}"
        t_inprog = f"{counts.get('in_progress', 0):,}"
        t_deleted = f"{counts.get('deleted', 0):,}"

        #st.markdown("<div class='db-section-title'>Knowledgebase Overview</div>", unsafe_allow_html=True)
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.markdown(f"<div class='kpi-card'><div class='kpi-title'>Total Knowledge</div><div class='kpi-row'><div class='kpi-value'>{t_total}</div><div class='kpi-icon'>📚</div></div></div>", unsafe_allow_html=True)
        with col2:
            st.markdown(f"<div class='kpi-card'><div class='kpi-title'>Active</div><div class='kpi-row'><div class='kpi-value'>{t_active}</div><div class='kpi-icon'>✅</div></div></div>", unsafe_allow_html=True)
        with col3:
            st.markdown(f"<div class='kpi-card'><div class='kpi-title'>In Progress</div><div class='kpi-row'><div class='kpi-value'>{t_inprog}</div><div class='kpi-icon'>⏳</div></div></div>", unsafe_allow_html=True)
        with col4:
            st.markdown(f"<div class='kpi-card'><div class='kpi-title'>Deleted</div><div class='kpi-row'><div class='kpi-value'>{t_deleted}</div><div class='kpi-icon'>🗑️</div></div></div>", unsafe_allow_html=True)

        st.divider()

        # Conversations summary and recent history (single row)
        conv_total, conv_recent = self._fetch_conversation_stats()
        #st.markdown("<div class='db-section-title'>Conversations</div>", unsafe_allow_html=True)
        aggs = self._fetch_client_aggregates()
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"<div class='kpi-card'><div class='kpi-title'>Conversations</div><div class='kpi-row'><div class='kpi-value'>{conv_total:,}</div><div class='kpi-icon'>💬</div></div></div>", unsafe_allow_html=True)
        with c2:
            st.markdown(f"<div class='kpi-card'><div class='kpi-title'>Hits</div><div class='kpi-row'><div class='kpi-value'>{aggs.get('total_requests', 0):,}</div><div class='kpi-icon'>📈</div></div></div>", unsafe_allow_html=True)
        with c3:
            st.markdown(f"<div class='kpi-card'><div class='kpi-title'>Users</div><div class='kpi-row'><div class='kpi-value'>{aggs.get('unique_users', 0):,}</div><div class='kpi-icon'>👤</div></div></div>", unsafe_allow_html=True)
        with c4:
            full_label = str(aggs.get("most_used_name") or (aggs.get("most_used_id") or "—"))
            short_label = full_label[:6]
            safe_full = html.escape(full_label)
            safe_short = html.escape(short_label)
            reqs = f"{aggs.get('most_used_requests', 0):,}"
            st.markdown(
                f"<div class='kpi-card'>"
                f"  <div class='kpi-title'>Most Used Knowledge</div>"
                f"  <div class='kpi-row'>"
                f"    <div>"
                f"      <div class='kpi-value' title='{safe_full}'>{safe_short}</div>"
                f"    </div>"
                f"    <div class='kpi-icon' title='{reqs} requests'>⬆️ {reqs}</div>"
                f"  </div>"
                f"</div>",
                unsafe_allow_html=True,
            )

       