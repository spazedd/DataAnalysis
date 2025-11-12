import os, json, sqlite3
from datetime import datetime
from urllib.parse import urlparse

import requests
import pandas as pd
import streamlit as st
import plotly.express as px

# ─────────────────────────────────────────────────────────────
# Optional: load .env during local dev (ignored on Streamlit Cloud)
# ─────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ─────────────────────────────────────────────────────────────
# App config
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Promptly Resumed — Research Lab", layout="wide")
st.title("Promptly Resumed — Research Lab")
st.caption("xAI-powered research with SQLite project logging and live results back to the site.")

# ─────────────────────────────────────────────────────────────
# Secrets / Env (ONLY XAI_API_KEY required)
# ─────────────────────────────────────────────────────────────
def get_secret(name: str, default: str = "") -> str:
    # 1) Streamlit Cloud Secrets
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    # 2) Environment variable
    val = os.getenv(name, default)
    return (val or "").strip()

XAI_API_KEY = get_secret("XAI_API_KEY")
XAI_API_BASE = "https://api.x.ai/v1"         # fixed default
XAI_MODEL    = "grok-4-0709"                 # fixed default
DB_PATH      = os.getenv("PR_DB_PATH", "research.db")

if not XAI_API_KEY:
    st.error(
        "Missing XAI_API_KEY.\n\n"
        "On Streamlit Cloud: go to **App → Settings → Secrets** and add:\n\n"
        "```\nXAI_API_KEY = <your_key_here>\n```\n"
    )
    st.stop()

# ─────────────────────────────────────────────────────────────
# SQLite connection and schema
# ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

conn = get_conn()

def ensure_schema():
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY,
        title TEXT UNIQUE,
        tags TEXT,
        notes TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS searches(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        query TEXT,
        result_json TEXT,
        ts TEXT,
        FOREIGN KEY(project_id) REFERENCES projects(id)
    )""")
    conn.commit()

ensure_schema()

# ─────────────────────────────────────────────────────────────
# Query params (new and legacy Streamlit APIs)
# ─────────────────────────────────────────────────────────────
def get_query_param(name: str, default: str = "") -> str:
    try:
        qp = st.query_params  # Streamlit >=1.33
        if isinstance(qp, dict):
            val = qp.get(name, [""])
            if isinstance(val, list):
                return (val[0] if val else "") or default
            return val or default
    except Exception:
        pass
    try:
        params = st.experimental_get_query_params()  # legacy
        val = params.get(name, [""])
        return (val[0] if val else "") or default
    except Exception:
        return default

# ─────────────────────────────────────────────────────────────
# xAI call
# ─────────────────────────────────────────────────────────────
def call_xai_web_search(topic: str) -> dict:
    """
    Calls xAI Chat Completions and expects JSON:
      {"summary": str, "results":[{"title","url","domain","date","snippet"}]}
    """
    system = (
        "You are a careful research assistant. Return STRICT JSON ONLY.\n"
        '{"summary": str, "results": [{"title": str, "url": str, "domain": str, "date": str, "snippet": str}]}\n'
        "Use credible outlets; include ISO dates when available. No extra prose."
    )
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Topic: {topic}\nReturn JSON only."}
        ],
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    r = requests.post(f"{XAI_API_BASE}/chat/completions", headers=headers, data=json.dumps(payload), timeout=60)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]

    # Parse JSON (strip code fences if present)
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
    elif isinstance(content, dict):
        data = content
    else:
        data = {"summary": "", "results": []}

    # Normalize
    norm = []
    for r in data.get("results", []):
        url = (r.get("url") or "").strip()
        try:
            dom = r.get("domain") or (urlparse(url).hostname or "")
        except Exception:
            dom = r.get("domain") or ""
        norm.append({
            "title": (r.get("title") or "")[:200],
            "url": url,
            "domain": dom,
            "date": r.get("date") or "Recent",
            "snippet": (r.get("snippet") or "")[:600],
        })
    data["results"] = norm[:10]
    return data

# ─────────────────────────────────────────────────────────────
# Post results back to parent iframe OR opener tab
# ─────────────────────────────────────────────────────────────
def send_results_back(results_dict: dict, origin: str = ""):
    """
    Posts results to:
      - parent iframe if embedded
      - else window.opener if opened in a new tab from the host page
    If `origin` is provided (from ?origin=...), we can target that specific origin.
    """
    payload = json.dumps(results_dict, ensure_ascii=False)
    target = json.dumps(origin or "*")
    st.components.v1.html(
        f"""
        <script>
        try {{
          const data = {{ type: "pr_results", payload: {payload} }};
          const target = {target};
          if (window.parent && window.parent !== window) {{
            window.parent.postMessage(data, target);
          }} else if (window.opener && !window.opener.closed) {{
            window.opener.postMessage(data, target);
          }}
        }} catch(e) {{ console.warn("postMessage failed", e); }}
        </script>
        """,
        height=0
    )

# ─────────────────────────────────────────────────────────────
# Sidebar: Projects
# ─────────────────────────────────────────────────────────────
st.sidebar.header("Projects")

with st.sidebar.expander("New / Edit Project", expanded=True):
    title = st.text_input("Title", key="p_title", placeholder="e.g., Semiconductor Export Controls")
    tags  = st.text_input("Tags (comma-separated)", key="p_tags", placeholder="economy, policy, tech")
    notes = st.text_area("Notes", key="p_notes", placeholder="Scope, assumptions, next steps…")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save / Update", use_container_width=True):
            if not title.strip():
                st.warning("Title is required.")
            else:
                now = datetime.utcnow().isoformat()
                conn.execute("""
                    INSERT INTO projects(title,tags,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(title) DO UPDATE SET
                      tags=excluded.tags,
                      notes=excluded.notes,
                      updated_at=excluded.updated_at
                """, (title.strip(), tags.strip(), notes.strip(), now, now))
                conn.commit()
                st.success("Project saved.")
    with c2:
        if st.button("Clear", use_container_width=True):
            for k in ("p_title","p_tags","p_notes"):
                if k in st.session_state: del st.session_state[k]
            st.experimental_rerun()

projects_df = pd.read_sql_query(
    "SELECT id, title, tags, notes, created_at, updated_at FROM projects ORDER BY updated_at DESC",
    conn
)
if projects_df.empty:
    st.sidebar.caption("No projects yet.")
else:
    pick = st.sidebar.selectbox("Open project", ["—"] + projects_df["title"].tolist())
    if pick and pick != "—":
        row = projects_df[projects_df["title"] == pick].iloc[0]
        st.sidebar.write(f"**Tags:** {row['tags'] or '-'}")
        st.sidebar.write(f"**Updated:** {row['updated_at'] or '-'}")
        c1, c2 = st.sidebar.columns(2)
        with c1:
            if st.button("Load to editor"):
                st.session_state["p_title"] = row["title"]
                st.session_state["p_tags"]  = row["tags"]
                st.session_state["p_notes"] = row["notes"]
                st.experimental_rerun()
        with c2:
            if st.button("Delete project"):
                conn.execute("DELETE FROM projects WHERE id=?", (int(row["id"]),))
                conn.commit()
                st.experimental_rerun()

# ─────────────────────────────────────────────────────────────
# Main: Research Console
# ─────────────────────────────────────────────────────────────
st.divider()
st.subheader("Research Console")

topic_prefill = get_query_param("q", "")
origin_hint   = get_query_param("origin", "")  # used to target postMessage

topic = st.text_input(
    "Research Topic",
    value=topic_prefill,
    placeholder="e.g., 50-year mortgages impact; semiconductor export controls"
)

colA, colB = st.columns([1, 2])
with colA:
    run = st.button("Run Web Search (xAI)", type="primary")
with colB:
    link_title = st.text_input("Attach to Project (optional: exact project title)")

result_obj = None

if run:
    if not topic.strip():
        st.warning("Enter a topic first.")
    else:
        with st.spinner("Querying xAI and assembling credible sources…"):
            try:
                result_obj = call_xai_web_search(topic.strip())
            except Exception as e:
                st.error(f"xAI error: {e}")
                result_obj = {"summary": "", "results": []}

        # Persist the run
        try:
            project_id = None
            if link_title.strip():
                row = pd.read_sql_query(
                    "SELECT id FROM projects WHERE title = ? LIMIT 1",
                    conn, params=[link_title.strip()]
                )
                if not row.empty:
                    project_id = int(row["id"].iloc[0])
            conn.execute(
                "INSERT INTO searches (project_id, query, result_json, ts) VALUES (?,?,?,?)",
                (project_id, topic.strip(), json.dumps(result_obj, ensure_ascii=False), datetime.utcnow().isoformat())
            )
            conn.commit()
        except Exception as e:
            st.error(f"DB insert failed: {e}")

        # Domain counts for site chart
        domains = [x.get("domain","") for x in result_obj.get("results", []) if x.get("domain")]
        if domains:
            counts_df = pd.Series(domains).value_counts().reset_index()
            counts_df.columns = ["domain","count"]
            domain_counts = counts_df.to_dict(orient="records")
        else:
            domain_counts = []

        # Post back (iframe or opener)
        send_results_back({
            "topic": topic.strip(),
            "summary": result_obj.get("summary",""),
            "results": result_obj.get("results", [])[:10],
            "charts": {"domainCounts": domain_counts}
        }, origin=origin_hint)

# ─────────────────────────────────────────────────────────────
# Results in-app
# ─────────────────────────────────────────────────────────────
st.divider()
st.subheader("Results")

if not result_obj:
    # Show last run if any
    recent = pd.read_sql_query("SELECT query, result_json, ts FROM searches ORDER BY ts DESC LIMIT 1", conn)
    if not recent.empty:
        topic = recent["query"].iloc[0]
        try:
            result_obj = json.loads(recent["result_json"].iloc[0])
        except Exception:
            result_obj = {"summary": "", "results": []}

if result_obj:
    if result_obj.get("summary"):
        st.markdown("**AI Summary**")
        st.write(result_obj["summary"])

    st.markdown("**Credible Sources**")
    for r in result_obj.get("results", []):
        title = r.get("title") or r.get("url") or "(untitled)"
        with st.expander(title[:90]):
            url = r.get("url","")
            dom = r.get("domain","")
            dt  = r.get("date","Recent")
            st.write(f"**Source**: [{dom}]({url})")
            st.write(f"**Published**: {dt}")
            if r.get("snippet"):
                st.write(r["snippet"])

    # Domain distribution chart
    domains = [x.get("domain","") for x in result_obj.get("results", []) if x.get("domain")]
    if domains:
        df = pd.Series(domains).value_counts().reset_index()
        df.columns = ["domain", "count"]
        fig = px.bar(df.head(10), x="domain", y="count", title="Top Domains")
        st.plotly_chart(fig, use_container_width=True)
else:
    st.caption("No results yet. Run a search above.")

# ─────────────────────────────────────────────────────────────
# History & Export
# ─────────────────────────────────────────────────────────────
st.divider()
st.subheader("History & Export")

hist = pd.read_sql_query("""
    SELECT s.id, s.query, s.ts, p.title as project_title
    FROM searches s
    LEFT JOIN projects p ON s.project_id = p.id
    ORDER BY s.ts DESC
    LIMIT 50
""", conn)

if not hist.empty:
    st.dataframe(hist, use_container_width=True)
    md_lines = ["# Research Highlights", ""]
    for _, row in hist.iterrows():
        q = row["query"]
        ts = row["ts"]
        ptitle = row["project_title"] or ""
        md_lines.append(f"- **{q}** — {ts}{' — Project: ' + ptitle if ptitle else ''}")
    md_blob = "\n".join(md_lines)
    st.download_button(
        "Download Highlights.md",
        data=md_blob.encode("utf-8"),
        file_name="Highlights.md",
        mime="text/markdown",
    )
else:
    st.caption("No search history yet.")

# ─────────────────────────────────────────────────────────────
# Diagnostics
# ─────────────────────────────────────────────────────────────
with st.expander("Diagnostics", expanded=False):
    st.write("DB:", DB_PATH)
    try:
        n_p = pd.read_sql_query("SELECT COUNT(*) c FROM projects", conn)["c"].iloc[0]
        n_s = pd.read_sql_query("SELECT COUNT(*) c FROM searches", conn)["c"].iloc[0]
        st.write(f"Projects: {n_p}  •  Searches: {n_s}")
    except Exception as e:
        st.error(e)
