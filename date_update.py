import os, json, time
import sqlite3
from datetime import datetime
import requests

DB_PATH = os.getenv("PR_DB_PATH", "research.db")
# Accept either name: X_API_TOKEN (X/Twitter) or XAI_API_TOKEN (user-provided name)
X_API_TOKEN = os.getenv("X_API_TOKEN") or os.getenv("XAI_API_TOKEN")
X_API_BASE = os.getenv("X_API_BASE", "https://api.x.com/2")

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")

# Create tables if missing
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
conn.commit()


def enabled_auto_projects():
    return conn.execute(
        "SELECT key, params_json FROM projects WHERE automated=1 AND enabled=1"
    ).fetchall()


def upsert_point(project_key: str, value: float, source: str, meta: dict):
    conn.execute(
        "INSERT INTO datapoints(project_key, ts, metric, value, source, meta_json) VALUES (?,?,?,?,?,?)",
        (project_key, datetime.utcnow().isoformat(), "value", float(value), source, json.dumps(meta or {})),
    )
    conn.commit()


# ---- X API helpers ----

def _x_request(url: str, params: dict):
    if not X_API_TOKEN:
        raise RuntimeError(
            "No API token found. Set X_API_TOKEN (or XAI_API_TOKEN) as a GitHub Actions secret."
        )
    headers = {"Authorization": f"Bearer {X_API_TOKEN}"}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    if r.status_code == 404 and "api.x.com" in url:
        # fallback to twitter domain if api.x.com not available
        alt = url.replace("api.x.com", "api.twitter.com")
        r = requests.get(alt, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_x_recent(query: str, max_results: int = 20):
    base = X_API_BASE.rstrip("/")
    url = f"{base}/tweets/search/recent"
    params = {
        "query": query,
        "max_results": min(max_results, 100),
        "tweet.fields": "created_at,public_metrics,lang",
    }
    return _x_request(url, params)


def score_from_engagement(tweet):
    pm = tweet.get("public_metrics", {})
    # lightweight heuristic; swap for a real sentiment model later
    return (
        pm.get("like_count", 0) * 0.001
        + pm.get("retweet_count", 0) * 0.002
        - pm.get("reply_count", 0) * 0.0005
    )


def ingest_x(project_key: str, params_json: str):
    try:
        params = json.loads(params_json or "{}")
    except Exception:
        params = {}
    query = params.get("x_query") or "(ai OR artificial intelligence) lang:en -is:retweet"
    data = fetch_x_recent(query, max_results=int(params.get("x_max", 25)))
    tweets = data.get("data", [])
    if not tweets:
        # Do nothing if no tweets; still record a sentinel for traceability
        upsert_point(project_key, 0.0, "x_api_none", {"query": query, "note": "no tweets"})
        return
    vals = [score_from_engagement(t) for t in tweets]
    agg = sum(vals) / max(len(vals), 1)
    meta = {"query": query, "count": len(tweets)}
    upsert_point(project_key, float(agg), "x_api", meta)


def main():
    rows = enabled_auto_projects()
    if not rows:
        print("No automated projects enabled.")
        return
    for key, params_json in rows:
        params = json.loads(params_json or "{}")
        # Only use X API path; skip everything else (per user's request)
        if params.get("x_query") or key.lower() == "sentiment":
            ingest_x(key, params_json)
            time.sleep(1)
        else:
            print(f"Skipping {key}: no x_query configured and non-X ingestors disabled.")

if __name__ == "__main__":
    main()
