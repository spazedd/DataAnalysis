#!/usr/bin/env python3
import os, sys, json, re, time, math, hashlib, datetime as dt
from typing import List, Dict
import requests
import xml.etree.ElementTree as ET
import yaml

ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG = os.path.join(ROOT, "configs", "interests.yml")
OUTDIR = os.path.join(ROOT, "data", "digest")

ARXIV_SEARCH = "https://export.arxiv.org/api/query?search_query={query}&start=0&max_results={n}&sortBy=submittedDate&sortOrder=descending"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
CROSSREF_WORKS = "https://api.crossref.org/works?query={query}&sort=published&order=desc&rows={n}"

RSS_FEEDS = [
    "https://www.nature.com/nature.rss",
    "https://www.science.org/rss/news_current.xml",
    "https://www.who.int/feeds/entity/mediacentre/news/en/rss.xml",
    "https://www.medrxiv.org/rss/current.xml",
    "https://www.biorxiv.org/rss/latest.xml",
]

def load_config():
    with open(CONFIG, "r") as f:
        return yaml.safe_load(f)

def now_utc():
    return dt.datetime.utcnow().replace(microsecond=0)

def days_ago(d: dt.datetime):
    return (now_utc() - d).days

def clean(text):
    return re.sub(r"\s+", " ", (text or "").strip())

def score_entry(entry, topics, allowlist, recency_days):
    s = 0
    title = (entry.get("title") or "").lower()
    abstr = (entry.get("abstract") or "").lower()
    url = entry.get("url","").lower()
    # topic hits
    for t in topics:
        if t.lower() in title: s += 4
        if t.lower() in abstr: s += 2
    # domain credibility
    if any(a in url for a in [d.lower() for d in allowlist]): s += 3
    # recency
    if entry.get("date"):
        try:
            d = dt.datetime.fromisoformat(entry["date"].replace("Z",""))
            if days_ago(d) <= recency_days: s += 3
        except Exception:
            pass
    # preprint de-boost if wanted
    if "biorxiv" in url or "medrxiv" in url:
        s += 1  # slight boost if allow_preprints=true (already default)
    return s

def parse_arxiv(xml_text):
    root = ET.fromstring(xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall("a:entry", ns):
        title = clean(e.findtext("a:title", default="", namespaces=ns))
        abstr = clean(e.findtext("a:summary", default="", namespaces=ns))
        link = ""
        for l in e.findall("a:link", ns):
            if l.attrib.get("rel") == "alternate":
                link = l.attrib.get("href", "")
        date = clean(e.findtext("a:published", default="", namespaces=ns)) or clean(e.findtext("a:updated", default="", namespaces=ns))
        # arXiv dates look like 2025-11-10T...
        out.append({"title": title, "abstract": abstr, "url": link or "", "source": "arXiv", "date": date})
    return out

def fetch_arxiv(q, n=20):
    url = ARXIV_SEARCH.format(query=requests.utils.quote(q), n=n)
    r = requests.get(url, timeout=30, headers={"User-Agent":"PromptlyResumed-Digest/1.0"})
    r.raise_for_status()
    return parse_arxiv(r.text)

def fetch_pubmed(q, n=20):
    # Search
    p = {"db":"pubmed","term":q,"retmax":n,"retmode":"json","sort":"pubdate"}
    r = requests.get(PUBMED_SEARCH, params=p, timeout=30)
    r.raise_for_status()
    ids = r.json().get("esearchresult",{}).get("idlist",[])
    if not ids: return []
    # Summary
    s = {"db":"pubmed","id":",".join(ids),"retmode":"json"}
    rr = requests.get(PUBMED_SUMMARY, params=s, timeout=30)
    rr.raise_for_status()
    out=[]
    for k,v in rr.json().get("result",{}).items():
        if k=="uids": continue
        title = clean(v.get("title"))
        url = f"https://pubmed.ncbi.nlm.nih.gov/{k}/"
        year = v.get("pubdate","").split(" ")[0]
        date = f"{year}-01-01T00:00:00" if year else ""
        abstr = clean(v.get("elocationid") or "")
        out.append({"title":title,"abstract":abstr,"url":url,"source":"PubMed","date":date})
    return out

def fetch_crossref(q, n=20):
    url = CROSSREF_WORKS.format(query=requests.utils.quote(q), n=n)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    items = r.json().get("message",{}).get("items",[])
    out=[]
    for it in items:
        title = clean(" ".join(it.get("title",[])))
        link = it.get("URL") or ""
        year = ""
        for k in ("published-print","published-online","created","deposited","issued"):
            if it.get(k,{}).get("date-parts"):
                y = it[k]["date-parts"][0][0]
                year = str(y); break
        date = f"{year}-01-01T00:00:00" if year else ""
        abstr = clean(it.get("abstract",""))
        out.append({"title":title,"abstract":abstr,"url":link,"source":"Crossref","date":date})
    return out

def fetch_rss(url):
    r = requests.get(url, timeout=30, headers={"User-Agent":"PromptlyResumed-Digest/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out=[]
    # try RSS 2.0
    for item in root.findall(".//item"):
        title = clean(item.findtext("title",""))
        link  = clean(item.findtext("link",""))
        date  = clean(item.findtext("pubDate","")) or clean(item.findtext("dc:date",""))
        out.append({"title":title,"abstract":"","url":link,"source":"RSS","date":""})
    return out

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    cfg = load_config()
    topics = cfg["topics"]
    allowlist = cfg.get("sources_allowlist",[])
    recency_days = cfg.get("boost",{}).get("recency_days",21)
    per_source = cfg.get("limit",{}).get("per_source",12)
    total_limit = cfg.get("limit",{}).get("total",30)

    candidates=[]
    queries = topics  # simple: query by topic terms
    # arXiv
    for q in queries:
        candidates += fetch_arxiv(q, per_source)
    # PubMed
    for q in queries:
        candidates += fetch_pubmed(q, per_source)
    # Crossref
    for q in queries:
        candidates += fetch_crossref(q, per_source)
    # RSS
    for feed in RSS_FEEDS:
        candidates += fetch_rss(feed)

    # score + dedupe
    seen=set(); items=[]
    for e in candidates:
        key = hashlib.sha1((e["title"]+e["url"]).encode()).hexdigest()
        if key in seen: continue
        seen.add(key)
        e["score"] = score_entry(e, topics, allowlist, recency_days)
        items.append(e)

    items.sort(key=lambda x: x["score"], reverse=True)
    items = items[:total_limit]

    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    out_json = os.path.join(OUTDIR, f"{today}.json")
    out_md   = os.path.join(OUTDIR, f"{today}.md")

    with open(out_json,"w") as f:
        json.dump({"date": today, "count": len(items), "items": items}, f, indent=2)

    # Simple Markdown brief
    lines = [f"# Promptly Resumed — Daily Digest ({today})", "", f"Top {len(items)} items across arXiv / PubMed / Crossref & feeds.", ""]
    for i,e in enumerate(items,1):
        lines.append(f"**{i}. {e['title']}**")
        if e.get("abstract"): lines.append(f"> {e['abstract'][:300]}{'…' if len(e['abstract'])>300 else ''}")
        lines.append(f"*Source:* {e['source']} | *Score:* {e['score']} | [Link]({e['url']})")
        lines.append("")
    with open(out_md,"w") as f:
        f.write("\n".join(lines))

    print(f"Wrote {out_json} and {out_md} ({len(items)} items).")

if __name__ == "__main__":
    main()
