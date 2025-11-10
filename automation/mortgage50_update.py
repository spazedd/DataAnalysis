# automation/mortgage50_update.py
import csv, io, os, sqlite3, json, re
from datetime import datetime, timedelta
import requests
from pathlib import Path

DB_PATH = os.getenv("PR_DB_PATH", "research.db")

FRED_SERIES = "MORTGAGE30US"  # 30-yr fixed
FRED_CSV = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_SERIES}"

PRINCIPALS = [200_000, 300_000, 400_000, 600_000, 800_000]
TERMS = [30, 40, 50]
PROJECT_KEY = "mortgage50_math"

def annuity_payment(P, r_annual, years):
    r = r_annual / 12.0
    n = years * 12
    return P * r * (1 + r)**n / ((1 + r)**n - 1)

def latest_fred_rate():
    r = requests.get(FRED_CSV, timeout=30)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    for row in reversed(rows):
        v = row.get(FRED_SERIES)
        if v and v not in ("", "."):
            return float(v) / 100.0
    raise RuntimeError("No usable FRED value found")

def ensure_tables(conn):
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

def seed_project(conn):
    params = {"notes": "30/40/50y payments from latest FRED 30y, plus ±1% scenarios"}
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
    """, (PROJECT_KEY,
          "50-Year Mortgage Scenarios",
          "Monthly payment & lifetime interest for 30/40/50y terms across principals and rates",
          1, 1, "daily", json.dumps(params)))

def put(conn, ts, metric, value, meta):
    conn.execute(
        "INSERT INTO datapoints(project_key, ts, metric, value, source, meta_json) VALUES (?,?,?,?,?,?)",
        (PROJECT_KEY, ts, metric, float(value), "fred+calc", json.dumps(meta))
    )

def main():
    base_rate = latest_fred_rate()
    scenarios = {
        "base": base_rate,
        "plus1pct": max(0.0, base_rate + 0.01),
        "minus1pct": max(0.0, base_rate - 0.01),
    }
    ts = datetime.utcnow().isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    ensure_tables(conn)
    seed_project(conn)

    for label, rate in scenarios.items():
        for P in PRINCIPALS:
            pmt = {yr: annuity_payment(P, rate, yr) for yr in TERMS}
            tot_int = {yr: pmt[yr] * yr * 12 - P for yr in TERMS}

            for yr in TERMS:
                meta = {"principal": P, "term_years": yr, "rate_decimal": rate, "scenario": label}
                put(conn, ts, f"pmt_{yr}y_{P}_{label}", pmt[yr], meta)
                put(conn, ts, f"interest_total_{yr}y_{P}_{label}", tot_int[yr], meta)

            m30, m40, m50 = pmt[30], pmt[40], pmt[50]
            i30, i40, i50 = tot_int[30], tot_int[40], tot_int[50]

            put(conn, ts, f"monthly_savings_40_vs_30_{P}_{label}", m30 - m40, {"principal": P, "rate_decimal": rate, "scenario": label})
            put(conn, ts, f"monthly_savings_50_vs_30_{P}_{label}", m30 - m50, {"principal": P, "rate_decimal": rate, "scenario": label})
            put(conn, ts, f"interest_penalty_40_vs_30_{P}_{label}", i40 - i30, {"principal": P, "rate_decimal": rate, "scenario": label})
            put(conn, ts, f"interest_penalty_50_vs_30_{P}_{label}", i50 - i30, {"principal": P, "rate_decimal": rate, "scenario": label})

            def pct(a, b): return (a / b - 1.0) * 100.0
            put(conn, ts, f"pmt_reduction_40_vs_30_pct_{P}_{label}", (1 - m40/m30)*100.0, {"principal": P, "rate_decimal": rate, "scenario": label})
            put(conn, ts, f"pmt_reduction_50_vs_30_pct_{P}_{label}", (1 - m50/m30)*100.0, {"principal": P, "rate_decimal": rate, "scenario": label})
            put(conn, ts, f"interest_increase_40_vs_30_pct_{P}_{label}", pct(i40, i30), {"principal": P, "rate_decimal": rate, "scenario": label})
            put(conn, ts, f"interest_increase_50_vs_30_pct_{P}_{label}", pct(i50, i30), {"principal": P, "rate_decimal": rate, "scenario": label})

    # --- JSON export (keep last 30 days) ---
    OUT_DIR = Path("data/mortgage"); OUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    json_path = OUT_DIR / f"mortgage_scenarios_{date_str}.json"

    rows = conn.execute(
        "SELECT * FROM datapoints WHERE project_key=? AND ts LIKE ?",
        (PROJECT_KEY, f"{date_str}%")
    ).fetchall()
    cols = [c[1] for c in conn.execute("PRAGMA table_info(datapoints)")]

    payload = [dict(zip(cols, r)) for r in rows]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    cutoff = (datetime.utcnow() - timedelta(days=30)).date()
    pat = re.compile(r"^mortgage_scenarios_(\d{4}-\d{2}-\d{2})\.json$")
    for p in OUT_DIR.glob("mortgage_scenarios_*.json"):
        m = pat.match(p.name)
        if not m: continue
        try: d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError: continue
        if d < cutoff:
            p.unlink(missing_ok=True)

    conn.commit()
    conn.close()
    print(f"✅ wrote scenarios at base_rate={base_rate:.3%} and exported JSON → {json_path}")
    
if __name__ == "__main__":
    main()