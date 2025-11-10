import os, json, time
import sqlite3
from datetime import datetime
import requests

DB_PATH = os.getenv("PR_DB_PATH", "research.db")

# xAI (console.x.ai) — ONLY this key is used
XAI_API_KEY = (os.getenv("XAI_API_KEY") or os.getenv("XAI_API_TOKEN") or "").strip()
XAI_API_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4-0709")  # latest Grok by default

if not XAI_API_KEY:
    raise RuntimeError("xAI API key missing. Set XAI_API_KEY in repo Secrets.")

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")

# Tables
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
)
""")
conn.execute("""
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
conn.commit()

def enabled_auto_projects():
    return conn.execute(
        "SELECT key, params_json FROM projects WHERE automated=1 AND enabled=1"
    ).fetchall()

def upsert_point(project_key: str, value: float, source: str, meta: dict, metric: str = "sentiment"):
    conn.execute(
        "INSERT INTO datapoints(project_key, ts, metric, value, source, meta_json) VALUES (?,?,?,?,?,?)",
        (project_key, datetime.utcnow().isoformat(), metric, float(value), source, json.dumps(meta or {})),
    )
    conn.commit()

# ---------- xAI helpers ----------
def _xai_chat_completion(query: str) -> dict:
    url = f"{XAI_API_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",  # REQUIRED for xAI
        "Content-Type": "application/json",
    }
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a scoring service. Given a topic/query, output STRICT JSON only:\n"
                    '{"score": <float -1.0..1.0>, "explain": "very short reason"}'
                ),
            },
            {"role": "user", "content": f"Query: {query}\nReturn JSON only."},
        ],
        "temperature": 0.2,
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    r.raise_for_status()
    return r.json()

def _extract_score_from_xai(resp: dict):
    try:
        content = resp["choices"][0]["message"]["content"]
        if isinstance(content, str):
            txt = content.strip()
            if txt.startswith("```"):
                txt = txt.strip("` \n")
                nl = txt.find("\n")
                if nl != -1:
                    txt = txt[nl+1:].strip()
            data = json.loads(txt)
        elif isinstance(content, dict):
            data = content
        else:
            data = {}

        score = float(data.get("score", 0.0))
        score = max(-1.0, min(1.0, score))  # clamp to [-1,1]
        meta = {"explain": data.get("explain", ""), "model": resp.get("model", XAI_MODEL)}
        return score, meta
    except Exception:
        return 0.0, {"raw": str(resp)[:500], "note": "parse_failed"}

def ingest_xai(project_key: str, params_json: str):
    try:
        params = json.loads(params_json or "{}")
    except Exception:
        params = {}
    query = params.get("x_query") or "(economy OR CPI)"
    resp = _xai_chat_completion(query)
    score, meta = _extract_score_from_xai(resp)
    upsert_point(project_key, score, "xai_chat", {"query": query, **meta}, metric="sentiment")

# ---------- main ----------
def main():
    rows = enabled_auto_projects()
    if not rows:
        print("No automated projects enabled.")
        return
    for key, params_json in rows:
        try:
            params = json.loads(params_json or "{}")
        except Exception:
            params = {}
        if params.get("x_query") or key.lower() == "sentiment":
            ingest_xai(key, params_json)
            time.sleep(1)
        else:
            print(f"Skipping {key}: no x_query configured (xAI-only).")

if __name__ == "__main__":
    main()