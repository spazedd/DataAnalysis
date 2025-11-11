import os, json, sqlite3, math, time
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt  # kept for your existing ecosystem
import plotly.express as px
import requests

DB_PATH = os.getenv("PR_DB_PATH", "research.db")

# -------------------- DB bootstrap --------------------
@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # existing tables (preserved)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY,
        key TEXT UNIQUE,
        name TEXT,
        description TEXT,
        enabled INTEGER DEFAULT 1,
        automated INTEGER DEFAULT 0,
        cadence TEXT DEFAULT 'daily',
        params_json TEXT DEFAULT '{}'
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS datapoints(
        id INTEGER PRIMARY KEY,
        project_key TEXT,
        ts TEXT,
        metric TEXT,
        value REAL,
        source TEXT,
        meta_json TEXT
    )""")
    # new table for search logs
    conn.execute("""
    CREATE TABLE IF NOT EXISTS searches(
        id INTEGER PRIMARY KEY,
        query TEXT,
        result_json TEXT,
        ts TEXT
    )""")
    conn.commit()
    return conn

conn = get_conn()

# -------------------- App config --------------------
st.set_page_config(page_title="Promptly Resumed — Research Lab", layout="wide")

# -------------------- PASSWORD GATE --------------------
if 'unlocked' not in st.session_state:
    st.session_state.unlocked = False
if 'rate_window' not in st.session_state:
    st.session_state.rate_window = int(time.time() // 3600)  # hour bucket
if 'rate_count' not in st.session_state:
    st.session_state.rate_count = 0

st.title("Promptly Resumed — Research Lab")
st.caption("SQL + Python + lightweight AI summaries. Toggle manual vs automation per project.")

with st.expander("🔐 Access", expanded=not st.session_state.unlocked):
    pw = st.text_input("Signal Key (Hint: logic + leet)", type="password")
    if st.button("Unlock"):
        if pw == "logic1337":
            st.session_state.unlocked = True
            st.balloons()
        else:
            st.error("Access Denied. The logic rejects you.")
if not st.session_state.unlocked:
    st.stop()

# -------------------- Sidebar: Admin (preserved) --------------------
st.sidebar.header("Admin")

with st.sidebar.expander("Project Controls", expanded=True):
    dfp = pd.read_sql_query(
        "SELECT key,name,enabled,automated,cadence,params_json FROM projects ORDER BY name",
        conn,
    )
    st.dataframe(dfp, use_container_width=True)

    st.subheader("Add / Update Project")
    c1, c2 = st.columns(2)
    with c1:
        key = st.text_input("Key (short id)")
        enabled = st.checkbox("Enabled", value=True)
        automated = st.checkbox("Automated (scheduler ingests)", value=False)
    with c2:
        name = st.text_input("Name")
        cadence = st.selectbox("Cadence", ["hourly", "daily", "weekly"], index=1)
    desc = st.text_area("Description")
    params = st.text_area(
        "Params JSON (optional)",
        value="{}",
        help='Example xAI: {"x_query": "(economy OR CPI) lang:en", "x_max": 50}',
    )

    if st.button("Save Project"):
        try:
            json.loads(params)
            conn.execute("""
                INSERT INTO projects(key,name,description,enabled,automated,cadence,params_json)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                  name=excluded.name,
                  description=excluded.description,
                  enabled=excluded.enabled,
                  automated=excluded.automated,
                  cadence=excluded.cadence,
                  params_json=excluded.params_json
            """, (key, name, desc, 1 if enabled else 0, 1 if automated else 0, cadence, params))
            conn.commit()
            st.success("Saved ✅")
        except Exception as e:
            st.error(f"Save failed: {e}")

# --- Quick diagnostics (preserved & slightly extended) ---
with st.expander("🔧 Debug: data connection", expanded=False):
    st.write("DB path:", DB_PATH)
    import os as _os
    exists = _os.path.exists(DB_PATH)
    size = _os.path.getsize(DB_PATH) if exists else 0
    st.write("Exists:", exists, "Size (bytes):", size)
    try:
        cnt_projects = pd.read_sql_query("SELECT COUNT(*) AS c FROM projects", conn)["c"].iloc[0]
        cnt_datapts = pd.read_sql_query("SELECT COUNT(*) AS c FROM datapoints", conn)["c"].iloc[0]
        st.write("Projects:", cnt_projects, "Datapoints:", cnt_datapts)
        distinct_keys = pd.read_sql_query(
            "SELECT project_key, COUNT(*) as n FROM datapoints GROUP BY project_key ORDER BY n DESC LIMIT 10", conn
        )
        st.dataframe(distinct_keys)
        latest = pd.read_sql_query("SELECT MAX(ts) AS ts FROM datapoints", conn)["ts"].iloc[0]
        st.write("Latest ts:", latest)
        # Searches debug
        cnt_searches = pd.read_sql_query("SELECT COUNT(*) AS c FROM searches", conn)["c"].iloc[0]
        st.write("Search logs:", cnt_searches)
    except Exception as e:
        st.error(f"DB query error: {e}")

st.divider()

# ==================== LOGIC LAB: Search & Credibility ====================
st.subheader("Logic Lab — Credible Web Search")

# --- Abuse control (simple, session-level rate limit) ---
def rate_ok(max_per_hour=5):
    now_bucket = int(time.time() // 3600)
    if now_bucket != st.session_state.rate_window:
        st.session_state.rate_window = now_bucket
        st.session_state.rate_count = 0
    return st.session_state.rate_count < max_per_hour

def bump_rate():
    st.session_state.rate_count += 1

query = st.text_input("Enter Research Topic", placeholder="e.g., 50-year mortgages impact, semiconductor export controls, etc.")
run_clicked = st.button("Run Web Search", type="primary")

XAI_API_KEY = (os.getenv("XAI_API_KEY") or os.getenv("XAI_API_TOKEN") or "").strip()
XAI_API_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4-0709")  # matches your updater defaults

def _call_xai_search(q: str) -> dict:
    """
    Direct call to xAI Chat Completions API (no paid middlemen).
    We instruct the model to return STRICT JSON with a top-level summary and a
    list of credible results (title/url/domain/date/snippet).
    """
    if not XAI_API_KEY:
        raise RuntimeError("xAI API key missing. Set XAI_API_KEY in repo Secrets or environment.")
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }
    system = (
        "You are a careful research assistant. Perform a web-oriented reasoning pass. "
        "Return STRICT JSON ONLY.\n"
        "Schema:\n"
        '{"summary": "2-3 paragraphs, neutral tone, bullet out key claims if helpful", '
        '"results": [{"title": str, "url": str, "domain": str, "date": str, "snippet": str}]}\n'
        "Prefer credible outlets. Include concrete dates in ISO format if available."
    )
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Topic: {q}\nReturn JSON only — no markdown or commentary."},
        ],
        "temperature": 0.2,
    }
    r = requests.post(f"{XAI_API_BASE}/chat/completions", headers=headers, data=json.dumps(payload), timeout=60)
    r.raise_for_status()
    data = r.json()
    # extract content; be robust to code fences
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, str):
        txt = content.strip()
        if txt.startswith("```"):
            txt = txt.strip("` \n")
            nl = txt.find("\n")
            if nl != -1:
                txt = txt[nl+1:].strip()
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = {"summary": txt, "results": []}
    else:
        parsed = content if isinstance(content, dict) else {"summary": "", "results": []}

    # Normalize results and domains
    norm_results = []
    for r in parsed.get("results", []):
        url = (r.get("url") or "").strip()
        try:
            dom = r.get("domain") or urlparse(url).hostname or ""
        except Exception:
            dom = r.get("domain") or ""
        norm_results.append({
            "title": r.get("title", "")[:200],
            "url": url,
            "domain": dom,
            "date": r.get("date") or "Recent",
            "snippet": (r.get("snippet") or "")[:600],
        })
    parsed["results"] = norm_results[:10]
    return parsed

if run_clicked:
    if not query.strip():
        st.warning("Enter a topic first.")
    elif not rate_ok():
        st.error("Rate limit: please wait before running more searches.")
    else:
        bump_rate()
        with st.spinner("Searching with xAI…"):
            try:
                result = _call_xai_search(query.strip())
            except Exception as e:
                st.error(f"xAI search error: {e}")
                result = {"summary": "", "results": []}

        # Show results
        st.subheader("AI Summary")
        st.write(result.get("summary", "No summary"))

        st.subheader("Credible Sources")
        for r in result.get("results", []):
            with st.expander(f"{r['title'][:80]}"):
                st.write(f"**Source**: [{r['domain']}]({r['url']})")
                st.write(f"**Published**: {r.get('date','Recent')}")
                st.write(r.get("snippet","") or "_No snippet_")

        # Save to DB
        try:
            conn.execute(
                "INSERT INTO searches (query, result_json, ts) VALUES (?,?,?)",
                (query.strip(), json.dumps(result, ensure_ascii=False), datetime.utcnow().isoformat())
            )
            conn.commit()
        except Exception as e:
            st.error(f"DB insert failed: {e}")

# ==================== CHARTS (recent activity) ====================
st.subheader("Recent Research Activity")

df = pd.read_sql_query(
    "SELECT id, query, result_json, ts FROM searches ORDER BY ts DESC LIMIT 50",
    conn
)
if not df.empty:
    # Extract domains for a quick frequency chart
    rows = []
    for _, row in df.iterrows():
        try:
            res = json.loads(row["result_json"])
        except Exception:
            res = {}
        for itm in res.get("results", []):
            dom = itm.get("domain") or ""
            if dom:
                rows.append({"query": row["query"], "domain": dom, "ts": row["ts"]})
    dfd = pd.DataFrame(rows)
    if not dfd.empty:
        top_domains = dfd["domain"].value_counts().head(10).reset_index()
        top_domains.columns = ["domain", "count"]
        fig = px.bar(top_domains, x="domain", y="count", title="Top domains (last 50 searches)")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No domains to chart yet. Run a search!")
else:
    st.caption("No searches logged yet. Run a search!")

# Footer
st.divider()
st.caption("Gated Logic Lab • xAI-powered summaries • SQLite logging • Plotly charts")
