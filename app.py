import os, json, sqlite3, math
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

# --- Quick diagnostics (collapsible) ---
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

# -------------------- Finance math helpers (for refi sim) --------------------
def pmt(P, r_annual, years):
    r = r_annual / 12.0
    n = years * 12
    return P * r * (1 + r)**n / ((1 + r)**n - 1)

def balance_after(P, r_annual, years, years_elapsed):
    """Remaining balance after years_elapsed payments on an amortizing loan."""
    r = r_annual / 12.0
    n = years * 12
    k = int(round(years_elapsed * 12))
    if k <= 0: 
        return P
    A = pmt(P, r_annual, years)
    # Closed form for remaining balance after k payments
    return P * (1 + r)**k - A * ((1 + r)**k - 1) / r

def total_interest_over_life(P, r_annual, years):
    return pmt(P, r_annual, years) * years * 12 - P

def interest_paid_until(P, r_annual, years, years_elapsed):
    A = pmt(P, r_annual, years)
    k = int(round(years_elapsed * 12))
    # Sum of payments made minus principal reduction = interest paid to date
    bal = balance_after(P, r_annual, years, years_elapsed)
    principal_repaid = P - bal
    return max(0.0, A * k - principal_repaid)

# =======================================================
#            50-Year Mortgage Scenarios (ENHANCED)
# =======================================================
st.subheader("50-Year Mortgage Scenarios")

principals = [200000, 300000, 400000, 600000, 800000]
principal = st.selectbox("Principal", principals, index=1)
scenario = st.selectbox("Rate scenario", ["base", "plus1pct", "minus1pct"], index=0)

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

rate_used = None

if latest_ts is None:
    st.info("No mortgage scenario data yet — run the GitHub Action once.")
else:
    # pull rate from meta_json so we can show it
    try:
        rate_row = pd.read_sql_query(
            f"""
            SELECT meta_json FROM datapoints
            WHERE project_key='mortgage50_math'
              AND meta_json LIKE '%"principal": {principal}%'
              AND meta_json LIKE '%"scenario": "{scenario}"%'
            LIMIT 1
            """, conn
        )
        if not rate_row.empty:
            meta = json.loads(rate_row["meta_json"].iloc[0])
            rate_used = meta.get("rate_decimal", None)
            if rate_used:
                st.markdown(f"**Rate used:** {rate_used * 100:.2f}% (source: FRED MORTGAGE30US — 30-year fixed)")
    except Exception as e:
        st.warning(f"Could not read rate info: {e}")

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

    m30 = get(f"pmt_30y_{principal}_{scenario}")
    m40 = get(f"pmt_40y_{principal}_{scenario}")
    m50 = get(f"pmt_50y_{principal}_{scenario}")
    i30 = get(f"interest_total_30y_{principal}_{scenario}")
    i40 = get(f"interest_total_40y_{principal}_{scenario}")
    i50 = get(f"interest_total_50y_{principal}_{scenario}")

    mons_40 = get(f"monthly_savings_40_vs_30_{principal}_{scenario}")
    mons_50 = get(f"monthly_savings_50_vs_30_{principal}_{scenario}")
    intp_40 = get(f"interest_penalty_40_vs_30_{principal}_{scenario}")
    intp_50 = get(f"interest_penalty_50_vs_30_{principal}_{scenario}")

    pct_pmt_40 = get(f"pmt_reduction_40_vs_30_pct_{principal}_{scenario}")
    pct_pmt_50 = get(f"pmt_reduction_50_vs_30_pct_{principal}_{scenario}")
    pct_int_40 = get(f"interest_increase_40_vs_30_pct_{principal}_{scenario}")
    pct_int_50 = get(f"interest_increase_50_vs_30_pct_{principal}_{scenario}")

    tbl = pd.DataFrame({
        "Term": ["30y", "40y", "50y"],
        "Monthly payment ($)": [m30, m40, m50],
        "Total interest ($)": [i30, i40, i50],
    })
    st.dataframe(tbl.style.format({"Monthly payment ($)": "{:,.0f}", "Total interest ($)": "{:,.0f}"}), use_container_width=True)

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

    fig, ax = plt.subplots()
    ax.bar(["30y", "40y", "50y"], [m30, m40, m50])
    ax.set_title(f"Monthly Payment by Term — ${principal:,} ({scenario})")
    ax.set_ylabel("USD / month")
    st.pyplot(fig)

    if None not in (pct_pmt_50, mons_50, pct_int_50, intp_50):
        story = (
            f"For a ${principal:,} loan in the **{scenario}** rate scenario"
            + (f" at **{rate_used*100:.2f}%**" if rate_used else "")
            + f": a 50-year cuts the monthly by **{pct_pmt_50:.1f}%** (~${mons_50:,.0f}/mo) "
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
                "rate_source": "FRED MORTGAGE30US (30-Year Fixed Mortgage Rate)",
                "rate_used_percent": rate_used * 100 if rate_used else None,
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
            st.download_button("Download JSON", data=json.dumps(out_json, indent=2),
                               file_name=f"mortgage_view_{principal}_{scenario}.json", mime="application/json")
        with colC:
            import io
            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight")
            st.download_button("Download chart (PNG)", data=buf.getvalue(),
                               file_name=f"mortgage_chart_{principal}_{scenario}.png", mime="image/png")

st.divider()

# =======================================================
#            Refi Ladder Simulator (NEW)
# =======================================================
st.subheader("Refi Ladder Simulator (50y → 30y/40y)")

c1, c2, c3 = st.columns(3)
with c1:
    P0 = st.number_input("Home price / Loan principal ($)", value=float(principal), step=10000.0, min_value=50000.0)
    start_term = st.selectbox("Start term (years)", [50, 40, 30], index=0)
with c2:
    start_rate_pct = st.number_input("Start rate (%)", value=round((rate_used or 0.069) * 100, 2), step=0.05)
    years_until_refi = st.slider("Years until refinance", 1, 15, 7)
with c3:
    refi_rate_pct = st.number_input("Refi rate (%)", value=5.00, step=0.05)
    refi_term = st.selectbox("Refi term (years)", [30, 40], index=0)

c4, c5, c6 = st.columns(3)
with c4:
    closing_costs_pct = st.number_input("Refi closing costs (% of balance)", value=2.0, step=0.25, min_value=0.0)
with c5:
    price_change_pct = st.number_input("Home price change at refi (%)", value=0.0, step=1.0)
with c6:
    run_sim = st.button("Run simulation")

def run_refi_sim():
    sr = start_rate_pct / 100.0
    rr = refi_rate_pct / 100.0

    # Phase 1: start loan until refi
    A0 = pmt(P0, sr, start_term)
    bal_at_refi = balance_after(P0, sr, start_term, years_until_refi)
    int_paid_phase1 = interest_paid_until(P0, sr, start_term, years_until_refi)

    # closing costs
    costs = bal_at_refi * (closing_costs_pct / 100.0)

    # Phase 2: new refi loan
    A1 = pmt(bal_at_refi, rr, refi_term)
    tot_int_phase2 = total_interest_over_life(bal_at_refi, rr, refi_term)

    # Baseline alternatives for comparison
    tot_int_stay_full = total_interest_over_life(P0, sr, start_term)  # never refi
    tot_paid_stay_full = tot_int_stay_full + P0

    # Total paid with refi plan
    tot_paid_refi = (
        A0 * years_until_refi * 12   # payments before refi
        + A1 * refi_term * 12        # payments after refi
        + costs
    )
    tot_int_refi = tot_paid_refi - P0

    # 30-year from start baseline at starting rate (strict comparison)
    A30_direct = pmt(P0, sr, 30)
    tot_paid_30_direct = A30_direct * 30 * 12
    tot_int_30_direct = tot_paid_30_direct - P0

    # Equity at refi (approx using price move)
    est_home_val = P0 * (1 + price_change_pct / 100.0)
    equity_at_refi = max(0.0, est_home_val - bal_at_refi)

    return {
        "A0": A0, "A1": A1,
        "bal_at_refi": bal_at_refi,
        "int_paid_phase1": int_paid_phase1,
        "closing_costs": costs,
        "tot_paid_refi": tot_paid_refi,
        "tot_int_refi": tot_int_refi,
        "tot_int_stay_full": tot_int_stay_full,
        "tot_paid_stay_full": tot_paid_stay_full,
        "A30_direct": A30_direct,
        "tot_int_30_direct": tot_int_30_direct,
        "equity_at_refi": equity_at_refi
    }

if run_sim:
    res = run_refi_sim()

    st.markdown("**Payments & balances**")
    tbl_refi = pd.DataFrame({
        "Item": [
            "Start monthly payment",
            "Remaining balance at refi",
            "Refi monthly payment",
            "Closing costs (est.)",
        ],
        "Value ($)": [
            res["A0"], res["bal_at_refi"], res["A1"], res["closing_costs"],
        ],
    })
    st.dataframe(tbl_refi.style.format({"Value ($)": "{:,.0f}"}), use_container_width=True)

    st.markdown("**Lifetime totals (compare plans)**")
    totals = pd.DataFrame({
        "Plan": ["Stay on start loan", "Refi plan", "30-year from start (same start rate)"],
        "Total paid ($)": [
            res["tot_paid_stay_full"],
            res["tot_paid_refi"],
            res["tot_int_30_direct"] + P0,
        ],
        "Total interest ($)": [
            res["tot_int_stay_full"],
            res["tot_int_refi"],
            res["tot_int_30_direct"],
        ],
    })
    st.dataframe(totals.style.format({"Total paid ($)": "{:,.0f}", "Total interest ($)": "{:,.0f}"}), use_container_width=True)

    # Bar chart of total interest by plan
    fig2, ax2 = plt.subplots()
    ax2.bar(
        ["Stay start loan", "Refi plan", "30y from start"],
        [res["tot_int_stay_full"], res["tot_int_refi"], res["tot_int_30_direct"]],
    )
    ax2.set_title("Total Interest by Plan")
    ax2.set_ylabel("USD")
    st.pyplot(fig2)

    # verdict text
    verdict = []
    if res["tot_int_refi"] < res["tot_int_stay_full"]:
        verdict.append("Refi saves interest vs staying on the original long loan.")
    else:
        verdict.append("Refi does NOT beat staying on the original long loan on total interest.")

    if res["tot_int_refi"] < res["tot_int_30_direct"]:
        verdict.append("Refi even beats starting on a 30-year at the same initial rate (rare).")
    else:
        verdict.append("Starting on a 30-year at the same initial rate still beats the refi ladder on lifetime interest.")

    verdict.append(f"Equity at refi (with price move): ~${res['equity_at_refi']:,.0f}.")
    verdict_text = " ".join(verdict)
    st.success("**Verdict:** " + verdict_text)

    # Downloads
    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "Download verdict (.txt)",
            data=verdict_text,
            file_name="refi_verdict.txt"
        )
    with col2:
        refi_json = {
            "inputs": {
                "principal": P0, "start_term": start_term, "start_rate_pct": start_rate_pct,
                "years_until_refi": years_until_refi, "refi_rate_pct": refi_rate_pct,
                "refi_term": refi_term, "closing_costs_pct": closing_costs_pct,
                "home_price_change_pct": price_change_pct
            },
            "results": res
        }
        st.download_button(
            "Download JSON",
            data=json.dumps(refi_json, indent=2),
            file_name="refi_simulation.json",
            mime="application/json"
        )
    with col3:
        import io
        buf2 = io.BytesIO()
        fig2.savefig(buf2, format="png", bbox_inches="tight")
        st.download_button("Download chart (PNG)", data=buf2.getvalue(),
                           file_name="refi_interest_chart.png", mime="image/png")

st.divider()

# -------------------- Enabled Projects (skip mortgage math project here) --------------------
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