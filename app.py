import os, json, sqlite3, time
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
import plotly.express as px
import requests

# -------------------- App config --------------------
st.set_page_config(page_title="Promptly Resumed — Research Lab", layout="wide")

# -------------------- PASSWORD GATE FIRST --------------------
if "unlocked" not in st.session_state:
    st.session_state.unlocked = False

st.title("Promptly Resumed — Research Lab")
st.caption("SQL + Python + lightweight AI summaries. Gated to keep it focused.")

with st.expander("🔐 Access", expanded=not st.session_state.unlocked):
    pw = st.text_input("Signal Key (Hint: logic + leet)", type="password")
    if st.button("Unlock"):
        if pw == "logic1337":
            st.session_state.unlocked = True
            st.balloons()
        else:
            st.error("Access Denied. The logic rejects you.")

if not st.session_state.unlocked:
    st.stop()  # <-- nothing else runs until unlocked

# -------------------- DB helpers (after gate) --------------------
DB_PATH = os.getenv("PR_DB_PATH", "research.db")

@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

conn = get_conn()

def ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()

    # robust names + no DEFAULT '{}' on TEXT for older SQLite builds
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY,
            proj_key TEXT UNIQUE,
            name TEXT,
            description TEXT,
            enabled INTEGER DEFAULT 1,
            automated INTEGER DEFAULT 0,
            cadence TEXT,
            params_json TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS datapoints(
            id INTEGER PRIMARY KEY,
            project_key TEXT,
            ts TEXT,
            metric TEXT,
            value REAL,
            source TEXT,
            meta_json TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS searches(
            id INTEGER PRIMARY KEY,
            query TEXT,
            result_json TEXT,
            ts TEXT
        )
    """)

    # Migration: if an old 'projects' table had a 'key' column, copy to 'proj_key'
    try:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(projects)").fetchall()}
        if "key" in cols and "proj_key" in cols:
            # fill proj_key if null
            cur.execute("UPDATE projects SET proj_key = COALESCE(proj_key, key)")
    except Exception:
        pass

    conn.commit()

ensure_schema(conn)

# -------------------- Sidebar: Admin (minimal but intact) --------------------
st.sidebar.header("Admin")

with st.sidebar.expander("Projects", expanded=True):
    dfp = pd.read_sql_query(
        "SELECT proj_key,name,enabled,automated,cadence,COALESCE(params_json,'{}') as params_json FROM projects ORDER BY name",
        conn,
    )
    st.dataframe(dfp, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        proj_key = st.text_input("Project key (short id)")
        enabled = st.checkbox("Enabled", value=True)
        automated = st.checkbox("Automated (scheduler ingests)", value=False)
    with c2:
        name = st.text_input("Name")
        cadence = st.selectbox("Cadence", ["hourly", "daily", "weekly"], index=1)
    desc = st.text_area("Description")
    params = st.text_area("Params JSON (optional)", value="{}")

    if st.button("Save Project"):
        try:
            json.loads(params)  # validate
            conn.execute("""
                INSERT INTO projects(proj_key,name,description,enabled,automated,cadence,params_json)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(proj_key) DO UPDATE SET
                  name=excluded.name,
                  description=excluded.description,
                  enabled=excluded.enabled,
                  automated=excluded.automated,
                  cadence=excluded.cadence,
                  params_json=excluded.params_json
            """, (proj_key, name, desc, 1 if enabled else 0, 1 if automated else 0, cadence, params))
            conn.commit()
            st.success("Saved ✅")
        except Exception as e:
            st.error(f"Save failed: {e}")

with st.expander("🔧 Debug", expanded=False):
    st.write("DB path:", DB_PATH)
    try:
        pcount = pd.read_sql_query("SELECT COUNT(*) c FROM projects", conn)["c"].iloc[0]
        dcount = pd.read_sql_query("SELECT COUNT(*) c FROM datapoints", conn)["c"].iloc[0]
        scount = pd.read_sql_query("SELECT COUNT(*) c FROM searches", conn)["c"].iloc[0]
        st.write(f"Projects={pcount}  Datapoints={dcount}  Searches={scount}")
    except Exception as e:
        st.error(e)

st.divider()

# -------------------- Logic Lab: Search + Credibility --------------------
st.subheader("Logic Lab — Credible Web Search")

# Optional deep-link: ?q=...
try:
    qparam = st.query_params.get("q", [""])[0]  # Streamlit ≥1.31
except Exception:
    qparam = ""

query = st.text_input("Enter Research Topic", value=qparam, placeholder="e.g., semiconductor export controls")
run_clicked = st.button("Run Web Search", type="primary")

XAI_API_KEY = (os.getenv("XAI_API_KEY") or os.getenv("XAI_API_TOKEN") or "").strip()
XAI_API_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4-0709")

def call_xai(q: str) -> dict:
    if not XAI_API_KEY:
        raise RuntimeError("xAI API key missing (set XAI_API_KEY)")
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    system = (
        "You are a careful research assistant. Return STRICT JSON ONLY.\n"
        '{"summary": str, "results": [{"title": str, "url": str, "domain": str, "date": str, "snippet": str}]}\n'
        "Prefer credible outlets; include ISO dates when available."
    )
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Topic: {q}\nReturn JSON only."}
        ],
        "temperature": 0.2,
    }
    r = requests.post(f"{XAI_API_BASE}/chat/completions", headers=headers, data=json.dumps(payload), timeout=60)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("` \n")
            nl = text.find("\n")
            if nl != -1:
                text = text[nl+1:].strip()
        try:
            data = json.loads(text)
        except Exception:
            data = {"summary": text, "results": []}
    else:
        data = content if isinstance(content, dict) else {"summary": "", "results": []}

    # Normalize
    out = []
    for r in data.get("results", []):
        url = (r.get("url") or "").strip()
        try:
            dom = r.get("domain") or urlparse(url).hostname or ""
        except Exception:
            dom = r.get("domain") or ""
        out.append({
            "title": (r.get("title") or "")[:200],
            "url": url,
            "domain": dom,
            "date": r.get("date") or "Recent",
            "snippet": (r.get("snippet") or "")[:600],
        })
    data["results"] = out[:10]
    return data

if run_clicked and query.strip():
    with st.spinner("Searching…"):
        try:
            result = call_xai(query.strip())
        except Exception as e:
            st.error(f"xAI search error: {e}")
            result = {"summary": "", "results": []}

    st.subheader("AI Summary")
    st.write(result.get("summary", "No summary."))

    st.subheader("Credible Sources")
    for r in result.get("results", []):
        with st.expander(r["title"][:80] or r["url"]):
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

# -------------------- Minimal recent-activity chart --------------------
st.subheader("Recent Activity")
df = pd.read_sql_query("SELECT id, query, result_json, ts FROM searches ORDER BY ts DESC LIMIT 50", conn)
if not df.empty:
    rows = []
    for _, row in df.iterrows():
        try:
            r = json.loads(row["result_json"])
        except Exception:
            r = {}
        for it in r.get("results", []):
            dom = it.get("domain") or ""
            if dom:
                rows.append({"domain": dom})
    dfd = pd.DataFrame(rows)
    if not dfd.empty:
        top = dfd["domain"].value_counts().head(10).reset_index()
        top.columns = ["domain", "count"]
        fig = px.bar(top, x="domain", y="count", title="Top domains (last 50 searches)")
        st.plotly_chart(fig, use_container_width=True)
else:
    st.caption("No searches logged yet.")

st.divider()
st.caption("Gated Logic Lab • xAI-powered summaries • SQLite logging • Plotly charts")
