"""Committee items from the House of Commons e-mail subscription.

The House's committee pages refuse automated readers, so there is no list of new news releases and
reports for the news job to check. The House will, however, e-mail that list to a subscriber. So the
pipeline has its own inbox: a private ntfy topic that accepts e-mail (ntfy-<topic>@ntfy.sh), given in
the COMMITTEE_INBOX secret and subscribed on the House website. Each run reads what has arrived.

The e-mail is only trusted as a list of document numbers. Every item is then read from
ourcommons.ca itself (single documents can be read), which is where the title, date and text come
from. Anything in an e-mail that is not a link to a House document is ignored, except that an
unrecognised message with a House link (such as the subscription's confirmation request) is passed
on to the phone once so it can be acted on.
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

OTTAWA = ZoneInfo("America/Toronto")
DOC_LINK = re.compile(r"https://www\.ourcommons\.ca/DocumentViewer/en/[\w\-/]{3,80}", re.I)
HOUSE_LINK = re.compile(r"https://www\.ourcommons\.ca/[^\s<>\"')\]]{3,300}", re.I)
KEEP_DAYS = 45
MAX_DOCS_PER_RUN = 12


def parse_mail(text: str):
    """[{url, committee, code, kind, hint}] for each House document linked in a subscription e-mail."""
    out, seen = [], set()
    committee = code = kind = ""
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        m = re.match(r"^(.{3,90}?)\s*\(([A-Z]{3,5})\)\s*$", line)
        if m and "http" not in line:
            committee, code, kind = m.group(1).strip(), m.group(2), ""
            continue
        if re.match(r"^[A-Za-z][A-Za-z ,'’\-]{2,40}:$", line):
            kind = line[:-1].strip()
            continue
        for link in DOC_LINK.finditer(line):
            url = link.group(0).rstrip("/.")
            if url.lower() in seen:
                continue
            seen.add(url.lower())
            hint = re.sub(r"[<\[(]\s*$", "", line[:link.start()]).strip(" -*•")
            out.append({"url": url, "committee": committee, "code": code, "kind": kind or "Item", "hint": hint[:200]})
    return out


def read_inbox(topic, server="https://ntfy.sh"):
    """Messages that reached the pipeline's inbox in the last 12 hours (as long as ntfy keeps them)."""
    req = urllib.request.Request(f"{server.rstrip('/')}/{topic}/json?poll=1&since=12h", headers={"User-Agent": "news-alerts"})
    with urllib.request.urlopen(req, timeout=20) as r:
        lines = r.read().decode("utf-8", "replace").splitlines()
    out = []
    for ln in lines:
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        if m.get("event") == "message":
            out.append({"id": m.get("id", ""), "time": m.get("time", 0), "title": m.get("title", ""), "text": m.get("message", "")})
    return out


MONTHS = {m: n for n, m in enumerate("january february march april may june july august september october november december".split(), 1)}


def parse_doc(page: str, url: str, fallback=None):
    """Title, date, committee and text of a House document page."""
    fallback = fallback or {}
    meta = re.search(r'<meta name="description" content="([^"]+)"', page)
    desc = html.unescape(meta.group(1)) if meta else ""
    inst = re.search(r'<meta name="Institution" content="([^"]+)"', page)
    kind = desc.split(" - ")[0].replace("Committee ", "").strip() if desc else fallback.get("kind", "Item")
    code = (re.search(r" - ([A-Z]{3,5}) \(\d+-\d+\)", desc) or [None, fallback.get("code", "")])[1]
    ts = 0
    d = re.search(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", desc)
    if d and d.group(1).lower() in MONTHS:
        ts = datetime(int(d.group(3)), MONTHS[d.group(1).lower()], int(d.group(2)), 12, tzinfo=OTTAWA).timestamp()
    body = re.sub(r"(?is)<(script|style|nav|head|header|footer)\b.*?</\1>", " ", page)
    body = re.sub(r"(?i)</?(p|div|br|li|h[1-6]|tr|td|section)\b[^>]*>", "\n", body)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in html.unescape(re.sub(r"<[^>]+>", " ", body)).split("\n")]
    lines = [ln for ln in lines if len(ln) > 2]
    start = max((i for i, ln in enumerate(lines) if re.match(r"(?i)^(for immediate release|news release|communiqu)", ln)), default=-1)
    text_lines = lines[start + 1:] if start >= 0 else lines
    for i, ln in enumerate(text_lines):   # stop at the contact block
        if re.match(r"(?i)^(for more information|- ?30 ?-|clerk of the|tel(ephone)?\b|e-?mail\b)", ln):
            text_lines = text_lines[:i]
            break
    # The headline is the first proper line of the release; the body is the paragraphs after it.
    dateline = re.compile(r"(?i)^(ottawa|[a-z]+ \d{1,2}, \d{4}|\d{4}-\d\d-\d\d)")
    head_at = next((i for i, ln in enumerate(text_lines) if 15 <= len(ln) <= 220 and not dateline.match(ln)), None)
    title = fallback.get("hint") or (text_lines[head_at] if head_at is not None else "")
    long_lines = [ln for ln in text_lines[(head_at + 1 if head_at is not None else 0):] if len(ln) > 60]
    return {"url": url, "title": (title or desc or "Committee item")[:220], "kind": kind,
            "code": code or "", "committee": (html.unescape(inst.group(1)) if inst else fallback.get("committee", "")),
            "ts": int(ts), "text": " ".join(long_lines)[:1500]}


def get_page(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-CA,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


def collect(state, now, na, notify, inbox=None, fetch=None):
    """New committee documents as news items, read from the inbox. Never raises."""
    topic = os.environ.get("COMMITTEE_INBOX", "").strip()
    box = state.setdefault("cmail", {"msgs": {}, "docs": {}})
    if not topic and inbox is None:
        return []
    try:
        messages = inbox if inbox is not None else read_inbox(topic)
    except Exception as e:
        print(f"committee inbox: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    fetch = fetch or (lambda url: get_page(url, na.BROWSER_UA))
    items, tried = [], 0
    for msg in messages:
        if not msg["id"] or msg["id"] in box["msgs"]:
            continue
        found = parse_mail(msg["text"])
        todo = [f for f in found if f["url"].lower() not in box["docs"]]
        if tried + len(todo) > MAX_DOCS_PER_RUN and tried:
            continue   # leave this message for the next run
        box["msgs"][msg["id"]] = int(now)
        if not found:
            # Not a list of items. If it carries a House link (a request to confirm the subscription,
            # say), pass it on once so it can be acted on; otherwise ignore it.
            link = HOUSE_LINK.search(msg["text"])
            if link and re.search(r"(?i)confirm|verif|activate|subscri|abonn", msg["text"] + msg["title"]):
                notify("Committee subscription: action needed",
                       (msg["title"] or "A message from the House of Commons subscription service") +
                       "\nTap to open the link it contains.", link.group(0).rstrip(".,"))
            continue
        for f in todo[:MAX_DOCS_PER_RUN]:
            tried += 1
            box["docs"][f["url"].lower()] = int(now)
            try:
                doc = parse_doc(fetch(f["url"]), f["url"], f)
            except Exception as e:
                print(f"committee document {f['url']}: {type(e).__name__}: {e}", file=sys.stderr)
                doc = {"url": f["url"], "title": f["hint"] or "Committee item", "kind": f["kind"], "code": f["code"],
                       "committee": f["committee"], "ts": 0, "text": ""}
            label = f"{doc['committee'] or doc['code']} committee" if (doc["committee"] or doc["code"]) else "Committee"
            items.append({
                "title": f"{label} {doc['kind'].lower()}: {doc['title']}", "link": doc["url"], "desc": doc["text"][:400],
                "source": "House of Commons committees", "source_url": "https://www.ourcommons.ca",
                "ts": float(doc["ts"] or now), "guid": doc["url"], "official": True,
                "context": "Canada federal Parliament committee", "max_age": 24 * 21, "committee_doc": doc})
    box["msgs"] = {k: v for k, v in box["msgs"].items() if now - v < 3 * 86400}
    box["docs"] = {k: v for k, v in box["docs"].items() if now - v < 120 * 86400}
    if items:
        print(f"committee inbox: {len(items)} new item(s)")
    return items


def record(news_path, items, now, na):
    """Keep every committee item, relevant or not, for the Parliament tab (parliament.json, "releases")."""
    if not items or not news_path:
        return
    path = os.path.join(os.path.dirname(news_path) or ".", "parliament.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {}
    rel = {r["url"]: r for r in data.get("releases", [])}
    for it in items:
        doc, topic = it["committee_doc"], na.classify(it)
        rel[doc["url"]] = {"url": doc["url"], "title": doc["title"], "kind": doc["kind"], "code": doc["code"],
                           "committee": doc["committee"], "ts": int(it["ts"]), "first_seen": int(now),
                           "assoc": topic["assoc"] if topic else "", "topic": topic["label"] if topic else "",
                           "text": doc["text"][:300]}
    data["releases"] = sorted((r for r in rel.values() if now - r["ts"] < KEEP_DAYS * 86400), key=lambda r: -r["ts"])[:80]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def debug(na, url):
    """Read one House document and print what was understood (used to check parsing from the runner)."""
    doc = parse_doc(get_page(url, na.BROWSER_UA), url)
    print("DOC", {k: (v if k != "text" else v[:500]) for k, v in doc.items()})
    return 0
