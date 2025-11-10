import os, json, sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

DB_PATH = os.getenv("PR_DB_PATH", "research.db")

# -------------------- DB bootstrap --------------------
@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
    return conn

conn = get_conn()

st.set_page_config(page_title="Promptly Resumed — Research Lab", layout="wide")
st.title("Promptly Resumed — Research Lab")
st.caption("SQL + Python + lightweight AI summaries. Toggle manual vs automation per project.")

# -------------------- Sidebar: Admin --------------------
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
            json.loads(params)  # validate JSON
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
            """, (key, name, desc, int(enabled), int(automated), cadence, params))
            conn.commit()
            st.success("Project saved.")
        except Exception as e:
            st.error(f"Error: {e}")

# -------------------- Helpers --------------------
def fetch_series(project_key: str, metric: str = "value") -> pd.DataFrame:
    q = """
        SELECT ts, value, source, meta_json
        FROM datapoints
        WHERE project_key=? AND metric=?
        ORDER BY ts
    """
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

def add_datapoint(project_key: str, value: float, source: str = "manual",
                  metric: str = "value", meta: dict | None = None):
    conn.execute(
        "INSERT INTO datapoints(project_key, ts, metric, value, source, meta_json) VALUES (?,?,?,?,?,?)",
        (project_key, datetime.utcnow().isoformat(), metric, float(value), source, json.dumps(meta or {})),
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

# =======================================================
#                 Mortgage Scenarios (NEW)
#   Requires: automation/mortgage50_update.py to have run
# =======================================================
st.subheader("50-Year Mortgage Scenarios")

principals = [200000, 300000, 400000, 600000, 800000]
principal = st.selectbox("Principal", principals, index=1)
scenario = st.selectbox("Rate scenario", ["base", "plus1pct", "minus1pct"], index=0)

# Get latest TS for this principal+scenario so we read one snapshot
latest_ts = pd.read_sql_query(
    f"""
    SELECT MAX(ts) AS ts
    FROM datapoints
    WHERE project_key='mortgage50_math'
      AND meta_json LIKE '%"principal": {principal}%'
      AND meta_json LIKE '%"scenario": "{scenario}"%'
    """,
    conn,
)["ts"].iloc[0]

if latest_ts is None:
    st.info("No mortgage scenario data yet — run the GitHub Action once.")
else:
    dfm = pd.read_sql_query(
        """
        SELECT metric, value
        FROM datapoints
        WHERE project_key='mortgage50_math' AND ts = ?
          AND meta_json LIKE ?
          AND meta_json LIKE ?
        """,
        conn,
        params=(latest_ts, f'%"principal": {principal}%', f'%"scenario": "{scenario}"%'),
    )

    def get(metric: str):
        row = dfm.loc[dfm["metric"] == metric]
        return float(row["value"].iloc[0]) if not row.empty else None

    # Payments / totals
    m30 = get(f"pmt_30y_{principal}_{scenario}")
    m40 = get(f"pmt_40y_{principal}_{scenario}")
    m50 = get(f"pmt_50y_{principal}_{scenario}")
    i30 = get(f"interest_total_30y_{principal}_{scenario}")
    i40 = get(f"interest_total_40y_{principal}_{scenario}")
    i50 = get(f"interest_total_50y_{principal}_{scenario}")

    # $ deltas
    mons_40 = get(f"monthly_savings_40_vs_30_{principal}_{scenario}")
    mons_50 = get(f"monthly_savings_50_vs_30_{principal}_{scenario}")
    intp_40 = get(f"interest_penalty_40_vs_30_{principal}_{scenario}")
    intp_50 = get(f"interest_penalty_50_vs_30_{principal}_{scenario}")

    # % deltas
    pct_pmt_40 = get(f"pmt_reduction_40_vs_30_pct_{principal}_{scenario}")
    pct_pmt_50 = get(f"pmt_reduction_50_vs_30_pct_{principal}_{scenario}")
    pct_int_40 = get(f"interest_increase_40_vs_30_pct_{principal}_{scenario}")
    pct_int_50 = get(f"interest_increase_50_vs_30_pct_{principal}_{scenario}")

    # Table: payments + totals
    tbl = pd.DataFrame({
        "Term": ["30y", "40y", "50y"],
        "Monthly payment ($)": [m30, m40, m50],
        "Total interest ($)": [i30, i40, i50],
    })
    st.dataframe(
        tbl.style.format({"Monthly payment ($)": "{:,.0f}", "Total interest ($)": "{:,.0f}"}),
        use_container_width=True
    )

    # Deltas table
    st.markdown("**Comparisons vs 30-year (same rate scenario):**")
    deltas = pd.DataFrame({
        "Metric": [
            "Monthly savings (40 vs 30) $",
            "Monthly savings (50 vs 30) $",
            "Interest increase (40 vs 30) $",
            "Interest increase (50 vs 30) $",
            "Monthly reduction (40 vs 30) %",
            "Monthly reduction (50 vs 30) %",
            "Interest increase (40 vs 30) %",
            "Interest increase (50 vs 30) %",
        ],
        "Value": [mons_40, mons_50, intp_40, intp_50, pct_pmt_40, pct_pmt_50, pct_int_40, pct_int_50],
    })
    st.dataframe(deltas.style.format({"Value": "{:,.2f}"}), use_container_width=True)

    # Chart: monthly payments by term
    fig, ax = plt.subplots()
    ax.bar(["30y", "40y", "50y"], [m30, m40, m50])
    ax.set_title(f"Monthly Payment by Term — ${principal:,} ({scenario})")
    ax.set_ylabel("USD / month")
    st.pyplot(fig)

    # One-liner takeaway
    if (pct_pmt_50 is not None) and (mons_50 is not None) and (pct_int_50 is not None) and (intp_50 is not None):
        st.info(
            f"At ${principal:,} in the **{scenario}** rate scenario, a 50-year lowers the monthly by "
            f"**{pct_pmt_50:.1f}%** (~${mons_50:,.0f}/mo) but increases lifetime interest by "
            f"**{pct_int_50:.1f}%** (~${intp_50:,.0f})."
        )

st.divider()

# -------------------- Enabled Projects --------------------
projects = pd.read_sql_query(
    "SELECT key,name,description FROM projects WHERE enabled=1 ORDER BY name",
    conn,
)

if projects.empty:
    st.info("No projects yet. Use the sidebar to add one (e.g., key=sentiment; params_json with x_query).")
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
st.markdown("Links: [okbutwhat.com](https://okbutwhat.com) • [somethingtoreport.com](https://somethingtoreport.com)")