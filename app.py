import streamlit as st
import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
import os, json

DB_PATH = os.getenv("PR_DB_PATH", "research.db")

@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY,
            key TEXT UNIQUE,
            name TEXT,
            description TEXT,
            enabled INTEGER DEFAULT 1,
            automated INTEGER DEFAULT 0,
            cadence TEXT DEFAULT 'daily',
            params_json TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS datapoints(
            id INTEGER PRIMARY KEY,
            project_key TEXT,
            ts TEXT,
            metric TEXT,
            value REAL,
            source TEXT,
            meta_json TEXT
        )
        """
    )
    return conn

conn = get_conn()
st.set_page_config(page_title="Promptly Resumed — Research Lab", layout="wide")
st.title("Promptly Resumed — Research Lab")
st.caption("SQL + Python + lightweight AI summaries. Toggle manual vs automation per project.")

# ---------- Admin Sidebar ----------
st.sidebar.header("Admin")
with st.sidebar.expander("Project Controls", expanded=True):
    dfp = pd.read_sql_query(
        "SELECT key,name,enabled,automated,cadence,params_json FROM projects ORDER BY name",
        conn,
    )
    st.dataframe(dfp, use_container_width=True)

    st.subheader("Add / Update Project")
    key = st.text_input("Key (short id)")
    name = st.text_input("Name")
    desc = st.text_area("Description")
    enabled = st.checkbox("Enabled", value=True)
    automated = st.checkbox("Automated (scheduler will ingest)", value=False)
    cadence = st.selectbox("Cadence", ["hourly", "daily", "weekly"], index=1)
    params = st.text_area(
        "Params JSON (optional)",
        value="{}",
        help='Example for X: {"x_query": "(economy OR CPI) lang:en -is:retweet", "x_max": 50}',
    )

    if st.button("Save Project"):
        try:
            json.loads(params)  # validate
            conn.execute(
                """
                INSERT INTO projects(key,name,description,enabled,automated,cadence,params_json)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                  name=excluded.name,
                  description=excluded.description,
                  enabled=excluded.enabled,
                  automated=excluded.automated,
                  cadence=excluded.cadence,
                  params_json=excluded.params_json
                """,
                (key, name, desc, int(enabled), int(automated), cadence, params),
            )
            conn.commit()
            st.success("Project saved.")
        except Exception as e:
            st.error(f"Error: {e}")

# ---------- Helpers ----------

def fetch_series(project_key: str, metric: str = "value") -> pd.DataFrame:
    q = (
        "SELECT ts, value, source, meta_json FROM datapoints WHERE project_key=? AND metric=? ORDER BY ts"
    )
    return pd.read_sql_query(q, conn, params=(project_key, metric))


def insight(df: pd.DataFrame) -> str:
    if df.empty:
        return "No data yet — add points or enable automation."
    d = df.copy()
    d["ts"] = pd.to_datetime(d["ts"], errors="coerce")
    trend = d["value"].diff().mean()
    avg = d["value"].mean()
    direction = "up" if (trend or 0) > 0 else "down"
    return f"Avg = {avg:.3f}. Recent trend: {direction}."


def add_datapoint(project_key: str, value: float, source: str = "manual", metric: str = "value", meta: dict | None = None):
    conn.execute(
        "INSERT INTO datapoints(project_key, ts, metric, value, source, meta_json) VALUES (?,?,?,?,?,?)",
        (
            project_key,
            datetime.utcnow().isoformat(),
            metric,
            float(value),
            source,
            json.dumps(meta or {}),
        ),
    )
    conn.commit()


def plot_series(df: pd.DataFrame, title: str):
    fig, ax = plt.subplots()
    if not df.empty:
        d = df.copy()
        d["ts"] = pd.to_datetime(d["ts"], errors="coerce")
        d.plot(x="ts", y="value", ax=ax)
    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel("Value")
    st.pyplot(fig)

# ---------- Main UI ----------
projects = pd.read_sql_query(
    "SELECT key,name,description FROM projects WHERE enabled=1 ORDER BY name",
    conn,
)

if projects.empty:
    st.info(
        "No projects yet. Use the sidebar to add one (e.g., key=sentiment; params_json with x_query)."
    )
else:
    for _, row in projects.iterrows():
        with st.expander(row["name"], expanded=True):
            st.write(row["description"])
            df = fetch_series(row["key"])  
            st.dataframe(df.tail(25), use_container_width=True)
            plot_series(df, f"{row['name']} — Value over Time")
            st.info(insight(df))

            st.markdown("**Add manual datapoint**")
            c1, c2, c3 = st.columns([1, 1, 1])
            with c1:
                val = st.number_input("value", value=1.0, step=0.1, key=f"val_{row['key']}")
            with c2:
                src = st.text_input("source", value="manual", key=f"src_{row['key']}")
            with c3:
                if st.button(f"Add to {row['key']}", key=f"btn_{row['key']}"):
                    add_datapoint(row["key"], val, source=src)
                    st.success("Added.")
                    st.rerun()

st.divider()
st.markdown(
    "Links: [okbutwhat.com](https://okbutwhat.com) • [somethingtoreport.com](https://somethingtoreport.com)"
