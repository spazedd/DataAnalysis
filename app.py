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

# --- Quick diagnostics (optional; collapse if you want) ---
with st.expander("🔧 Debug: data connection", expanded=False):
    import os as _os
    st.write("DB path:", DB_PATH)
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
    except Exception as e:
        st.error(f"DB query error: {e}")

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
#            50-Year Mortgage Scenarios (ENHANCED)
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

    # Narrative takeaway + downloads
    if None not in (pct_pmt_50, mons_50, pct_int_50, intp_50):
        story = (
            f"For a ${principal:,} loan in the **{scenario}** rate scenario: "
            f"a 50-year cuts the monthly by **{pct_pmt_50:.1f}%** (~${mons_50:,.0f}/mo) "
            f"but increases lifetime interest by **{pct_int_50:.1f}%** (~${intp_50:,.0f}). "
            f"The 40-year sits in between (monthly −{pct_pmt_40:.1f}%, interest +{pct_int_40:.1f}%). "
            "In a supply-constrained market, that mostly expands what buyers can bid rather than the number of homes—"
            "so it risks higher prices while transferring much more to lenders."
        )
        st.success(story)

        colA, colB, colC = st.columns(3)
        with colA:
            st.download_button("Download summary (.txt)", data=story, file_name=f"mortgage_summary_{principal}_{scenario}.txt")
        with colB:
            out_json = {
                "principal": principal,
                "scenario": scenario,
                "monthly": {"30y": m30, "40y": m40, "50y": m50},
                "interest_total": {"30y": i30, "40y": i40, "50y": i50},
                "deltas": {
                    "monthly_savings_vs_30": {"40y": mons_40, "50y": mons_50},
                    "interest_increase_vs_30": {"40y": intp_40, "50y": intp_50},
                    "monthly_reduction_pct_vs_30": {"40y": pct_pmt_40, "50y": pct_pmt_50},
                    "interest_increase_pct_vs_30": {"40y": pct_int_40, "50y": pct_int_50},
                },
                "timestamp": latest_ts,
            }
            st.download_button(
                "Download JSON", data=json.dumps(out_json, indent=2),
                file_name=f"mortgage_view_{principal}_{scenario}.json", mime="application/json"
            )
        with colC:
            import io
            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight")
            st.download_button("Download chart (PNG)", data=buf.getvalue(),
                               file_name=f"mortgage_chart_{principal}_{scenario}.png", mime="image/png")

st.divider()

# -------------------- Enabled Projects (skip mortgage summary project here) --------------------
projects = pd.read_sql_query(
    "SELECT key,name,description FROM projects WHERE enabled=1 AND key!='mortgage50_math' ORDER BY name",
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