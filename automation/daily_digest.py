# --- top of file imports stay the same ---
import os, time, math, json, re, datetime as dt
import requests
# ...

UA = {"User-Agent": "PromptlyResumed-Digest/1.0 (+https://promptlyresumed.com)"}
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "")
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "")

def polite_get(url, params=None, headers=None, retries=4, backoff=2.0, min_delay=0.35):
    """GET with UA, tiny delay, and 429 backoff."""
    time.sleep(min_delay)  # avoid bursts
    hdrs = {}
    hdrs.update(UA)
    if headers:
        hdrs.update(headers)
    for attempt in range(retries):
        r = requests.get(url, params=params, headers=hdrs, timeout=30)
        if r.status_code == 429:
            sleep_s = backoff * (attempt + 1)
            time.sleep(sleep_s)
            continue
        r.raise_for_status()
        return r
    # last try
    r.raise_for_status()
    return r

# ---- PubMed helpers ----
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

def fetch_pubmed(q, n=20):
    """Search PubMed, then batch esummary (max 100 IDs per call)."""
    # search
    p = {
        "db": "pubmed",
        "term": q,
        "retmax": n,
        "retmode": "json",
        "sort": "pubdate",
        "tool": "promptlyresumed",
        "email": NCBI_EMAIL or "contact@promptlyresumed.com",
    }
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY

    r = polite_get(PUBMED_SEARCH, params=p)
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    out = []
    # batch in chunks of 100
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        s = {
            "db": "pubmed",
            "id": ",".join(chunk),
            "retmode": "json",
            "tool": "promptlyresumed",
            "email": NCBI_EMAIL or "contact@promptlyresumed.com",
        }
        if NCBI_API_KEY:
            s["api_key"] = NCBI_API_KEY

        rr = polite_get(PUBMED_SUMMARY, params=s)
        j = rr.json()
        for k, v in j.get("result", {}).items():
            if k == "uids": 
                continue
            title = (v.get("title") or "").strip()
            url = f"https://pubmed.ncbi.nlm.nih.gov/{k}/"
            pubdate = (v.get("pubdate") or "").strip()
            year = pubdate.split(" ")[0] if pubdate else ""
            date = f"{year}-01-01T00:00:00" if year else ""
            abstr = (v.get("elocationid") or "").strip()
            out.append({
                "title": title,
                "abstract": abstr,
                "url": url,
                "source": "PubMed",
                "date": date
            })
    return out

# ---- arXiv / Crossref / RSS use polite_get with short sleeps ----

def fetch_arxiv(q, n=20):
    url = f"https://export.arxiv.org/api/query?search_query={requests.utils.quote(q)}&start=0&max_results={n}&sortBy=submittedDate&sortOrder=descending"
    r = polite_get(url, min_delay=0.75)  # arXiv is sensitive; be polite
    return parse_arxiv(r.text)

def fetch_crossref(q, n=20):
    url = "https://api.crossref.org/works"
    params = {"query": q, "sort": "published", "order": "desc", "rows": n, "mailto": (NCBI_EMAIL or "contact@promptlyresumed.com")}
    r = polite_get(url, params=params, min_delay=0.35)
    items = r.json().get("message", {}).get("items", [])
    out = []
    for it in items:
        title = " ".join(it.get("title", [])).strip()
        link = it.get("URL") or ""
        year = ""
        for k in ("published-print","published-online","created","deposited","issued"):
            if it.get(k, {}).get("date-parts"):
                year = str(it[k]["date-parts"][0][0]); break
        date = f"{year}-01-01T00:00:00" if year else ""
        abstr = (it.get("abstract", "") or "").strip()
        out.append({"title": title, "abstract": abstr, "url": link, "source": "Crossref", "date": date})
    return out

def fetch_rss(url):
    r = polite_get(url, min_delay=0.5)
    # parse as before...
    # (keep your existing RSS parsing logic)
