import os, json, sqlite3
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
import plotly.express as px

st.set_page_config(page_title="Promptly Resumed — Research Lab", layout="wide")
st.title("Promptly Resumed — Research Lab")

# === API KEY (only this is required) ===
XAI_API_KEY = (os.getenv("XAI_API_KEY") or "").strip()
if not XAI_API_KEY:
    st.error("Missing XAI_API_KEY in environment or secrets.")
    st.stop()

# === SQLite ===
DB_PATH = "research.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS searches(
    id INTEGER PRIMARY KEY,
    query TEXT,
    result_json TEXT,
    ts TEXT
)""")
conn.commit()

# === xAI Call ===
def run_xai(topic: str) -> dict:
    system = (
        "You are a research assistant. Return STRICT JSON ONLY.\n"
        '{"summary": str, "results":[{"title":str,"url":str,"domain":str,"date":str,"snippet":str}]}\n'
        "Use credible outlets; include ISO dates when possible."
    )
    payload = {
        "model": "grok-4-0709",
        "messages": [
            {"role":"system","content":system},
            {"role":"user","content":f"Topic: {topic}\nReturn JSON only."}
        ],
        "temperature":0.2
    }
    headers = {"Authorization":f"Bearer {XAI_API_KEY}","Content-Type":"application/json"}
    r = requests.post("https://api.x.ai/v1/chat/completions",headers=headers,data=json.dumps(payload),timeout=60)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    try:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("` \n")
            nl = text.find("\n")
            if nl!=-1: text=text[nl+1:].strip()
        data = json.loads(text)
    except Exception:
        data = {"summary": text, "results": []}

    # normalize
    out=[]
    for r in data.get("results",[]):
        url=r.get("url") or ""
        dom=r.get("domain") or (urlparse(url).hostname or "")
        out.append({
            "title":r.get("title",""),
            "url":url,
            "domain":dom,
            "date":r.get("date","Recent"),
            "snippet":r.get("snippet","")
        })
    data["results"]=out[:10]
    return data

# === postMessage helper ===
def send_results_back(results_dict):
    payload=json.dumps(results_dict,ensure_ascii=False)
    st.components.v1.html(
        f"""
        <script>
        try {{
          if(window.parent) {{
            window.parent.postMessage({{
              type:"pr_results",
              payload:{payload}
            }},"*");
          }}
        }}catch(e){{console.warn(e)}}
        </script>
        """,
        height=0
    )

# === UI ===
topic = st.text_input("Research Topic", placeholder="e.g. 50-year mortgages impact; semiconductor export controls")
run = st.button("Run Analysis", type="primary")

if run and topic.strip():
    with st.spinner("Querying xAI…"):
        result = run_xai(topic.strip())

    # store to DB
    conn.execute("INSERT INTO searches(query,result_json,ts) VALUES(?,?,?)",
                 (topic.strip(),json.dumps(result,ensure_ascii=False),datetime.utcnow().isoformat()))
    conn.commit()

    # compute domain frequency
    domains=[r.get("domain","") for r in result.get("results",[]) if r.get("domain")]
    df=pd.Series(domains).value_counts().reset_index()
    df.columns=["domain","count"]

    # send to parent site
    send_results_back({
        "topic":topic.strip(),
        "summary":result.get("summary",""),
        "results":result.get("results",[])[:10],
        "charts":{"domainCounts":df.to_dict(orient="records")}
    })

    # display in-app
    st.markdown("### AI Summary")
    st.write(result.get("summary",""))
    st.markdown("### Credible Sources")
    for r in result.get("results",[]):
        with st.expander(r.get("title") or r.get("url")):
            st.write(f"**Source:** [{r.get('domain')}]({r.get('url')})")
            st.write(f"**Published:** {r.get('date')}")
            st.write(r.get("snippet",""))
    if not df.empty:
        fig=px.bar(df.head(10),x="domain",y="count",title="Top Domains")
        st.plotly_chart(fig,use_container_width=True)

else:
    st.caption("Enter a topic and click Run Analysis.")