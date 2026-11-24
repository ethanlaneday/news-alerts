"""Data for the page's Parliament view, written to parliament.json beside news.json.

Three lists, refreshed about once an hour:
  bills     tracked bills and how far each has got (LEGISinfo)
  meetings  upcoming meetings of the House committees we watch, with their agendas
            (schedule from openparliament.ca, agenda from the notice on ourcommons.ca)
  mentions  what was said in the House and committees on our topics (openparliament.ca search)
"""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

OTTAWA = ZoneInfo("America/Toronto")
API = "https://api.openparliament.ca"
BILLS_URL = "https://www.parl.ca/legisinfo/en/bills/json"
REFRESH_MIN = 55
MEETING_DAYS_AHEAD = 14
MENTION_DAYS = 21
AGENDA_REFRESH_H = 3

# House committees to list, and whose work they mostly touch. Agenda wording can override this.
WATCH = {"HESA": "HEAL", "CIIT": "CADA", "INDU": "CADA", "TRAN": "CADA", "ENVI": "CADA", "FINA": "BOTH"}
# Bills to follow whatever their title says, e.g. "C-12". Sponsors are matched on their name.
TRACK_BILLS = set()
TRACK_SPONSORS = {"michael ma": "michaelma"}
# Bill titles are short and formal, so they get their own word lists (the news filters are too loose for them).
BILL_WORDS = {
    "CADA": r"motor vehicle|automobile|automotive|\bvehicles?\b|customs tariff|\btariffs?\b|cusma|free trade|trade agreement|"
            r"competition act|consumer (protection|rights|data)|used car|right to repair|excise|luxury tax|zero.emission|carbon|fuel|bank act|interest rate",
    "HEAL": r"health|medical|medicine|pharmacare|pharmac|\bdrugs?\b(?! traffick)|dental|nurs|physician|doctor|patient|"
            r"opioid|tobacco|vaping|disabilit|seniors|(long-term|palliative|primary|connected|home) care|artificial intelligence|\bAI\b",
    "BOTH": r"budget|economic statement",
}
# Organisations worth flagging when they appear before a committee.
WITNESS_WORDS = (r"automobile dealers|vehicle manufacturers|global automakers|automotive parts|\bunifor\b|"
                 r"organizations for health action|canadian medical association|canadian nurses association|healthcarecan|"
                 r"canadian pharmacists|canadian dental|canadian psychological|mental health commission|"
                 r"canadian institute for health information|canada health infoway")


def get_json(na, url):
    return json.loads(na.fetch(url, tries=2).decode("utf-8", "replace"))


def get_page(na, url) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": na.BROWSER_UA, "Accept": "text/html,*/*;q=0.8",
                                               "Accept-Language": "en-CA,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(3_000_000).decode("utf-8", "replace")


def topic_for(na, text):
    return na.classify({"title": text, "desc": "", "title_only": True, "context": "Canada federal Parliament"})


def iso_ts(s) -> int:
    if not s or str(s).startswith("0001"):
        return 0
    try:
        d = datetime.fromisoformat(str(s)[:19])
        return int(d.replace(tzinfo=OTTAWA).timestamp())
    except ValueError:
        return 0


# ---------------------------------------------------------------- bills
def bill_steps(b):
    """Seven steps in the order this bill takes them: three readings in each chamber, then Royal Assent."""
    house = [b.get(f"PassedHouse{n}ReadingDateTime") for n in ("First", "Second", "Third")]
    senate = [b.get(f"PassedSenate{n}ReadingDateTime") for n in ("First", "Second", "Third")]
    first, second = (senate, house) if b.get("IsSenateBill") else (house, senate)
    return [iso_ts(x) for x in first + second + [b.get("ReceivedRoyalAssentDateTime")]]


def read_bills(na):
    out = []
    for b in get_json(na, BILLS_URL):
        num = (b.get("NumberCode") or "").strip()
        long_t, short_t = (b.get("LongTitleEn") or "").strip(), (b.get("ShortTitleEn") or "").strip()
        sponsor = re.sub(r"\s+", " ", b.get("SponsorPersonName") or "").strip()
        if not num or not long_t:
            continue
        topic = None
        for name, key in TRACK_SPONSORS.items():
            if name in sponsor.lower():
                topic = topic_for(na, "") or next((t for t in na.TOPICS if t["key"] == key), None)
        text = f"{long_t} {short_t}"
        text = re.sub(r"(?i)\b(soil|animal|plant|forest|ocean) health\b|child care", " ", text)
        assoc = next((a for a in ("BOTH", "CADA", "HEAL") if re.search(BILL_WORDS[a], text, re.I)), "")
        if not (topic or assoc or num in TRACK_BILLS):
            continue
        guess = topic_for(na, text)
        if not topic and guess and guess["assoc"] == assoc:
            topic = guess
        steps = bill_steps(b)
        done = max((i + 1 for i, t in enumerate(steps) if t), default=0)
        last = max(steps + [iso_ts(b.get("LatestBillEventDateTime")), iso_ts(b.get("LatestCompletedBillStageDateTime"))])
        riding = (b.get("SponsorConstituencyNameEn") or "").strip()
        role = (b.get("SponsorAffiliationTitleEn") or "").strip()
        out.append({
            "num": num, "title": short_t or long_t, "long": long_t if short_t else "",
            "type": (b.get("BillDocumentTypeNameEn") or "").strip(),
            "sponsor": sponsor + (f" ({role or riding})" if sponsor and (role or riding) else ""),
            "senate": bool(b.get("IsSenateBill")), "done": done,
            "status": (b.get("StatusNameEn") or b.get("LatestCompletedMajorStageNameWithChamberSuffix") or "").strip(),
            "changed": last,
            "assoc": topic["assoc"] if topic else (assoc or "BOTH"),
            "topic": topic["label"] if topic else {"CADA": "Auto and trade", "HEAL": "Health", "BOTH": "Budget"}.get(assoc, "Tracked"),
            "link": f"https://www.parl.ca/legisinfo/en/bill/{b.get('ParliamentNumber')}-{b.get('SessionNumber')}/{num.lower()}",
        })
    out.sort(key=lambda x: -x["changed"])
    return out[:60]


# ---------------------------------------------------------------- committee meetings
BLOCK = r"(?i)</?(?:p|div|br|li|ul|ol|tr|td|th|table|h[1-6]|section|article|header|footer|span\s+class=\"[^\"]*(?:Witness|Organization)[^\"]*\")\b[^>]*>"
SKIP_LINE = re.compile(
    r"(?i)^(notice of meeting|amended|(standing|special|joint|sub).*committee.*|\d+(st|nd|rd|th) parliament.*|"
    r"(witness(es)?|televised|webcast|in camera|public)( amended)?|meeting \d+|meeting no\.? ?\d+|televised|webcast|in camera|\d{1,2}:\d\d ?[ap]\.?m\.?.*|"
    r"(mon|tues|wednes|thurs|fri|satur|sun)day,.*|room .*|.* block$|.*building.*|witness(es)?|videoconference|"
    r"by videoconference|\(public\)|public|amended section|\*+.*|to be determined|.*sir john a\. macdonald.*)$")


def notice_lines(page: str):
    """The agenda part of a Notice of Meeting page, as plain lines."""
    page = re.sub(r"(?is)<(script|style|nav|head)\b.*?</\1>", " ", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(BLOCK, "\n", page)))
    lines = [re.sub(r"\s+", " ", ln).strip(" •·-") for ln in text.split("\n")]
    lines = [ln for ln in lines if len(ln) > 1]
    starts = [i for i, ln in enumerate(lines) if re.match(r"(?i)^(amended )?notice of meeting", ln)]
    if not starts:
        return []
    body = lines[starts[-1] + 1:]
    for i, ln in enumerate(body):
        if re.match(r"(?i)^(clerks? of the (sub)?committee|.*\(613[-\) ]|\d{4}-\d\d-\d\d \d|[\d/-]+ \d{1,2}:\d\d ?[ap]\.?m\.?$)", ln):
            body = body[:i]
            break
    seen, out = set(), []
    for ln in body:
        if ln.lower() in seen:
            continue
        seen.add(ln.lower())
        out.append(re.sub(r"\s*\(\s*by videoconference\s*\)", "", ln, flags=re.I)[:180])
    return out[:60]


def agenda_of(lines):
    """(subject, the other agenda lines) with dates, rooms and headings removed."""
    keep = [ln for ln in lines if not SKIP_LINE.match(ln)]
    return (keep[0] if keep else ""), keep[1:25]


def read_meetings(na, old, now):
    cache = {m["url"]: m for m in old.get("meetings", [])}
    acr = dict(old.get("acronyms") or {})
    today = datetime.fromtimestamp(now, OTTAWA).date()
    rows = get_json(na, f"{API}/committees/meetings/?format=json&limit=150").get("objects", [])
    out = []
    for r in rows:
        try:
            day = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if not today <= day <= today + timedelta(days=MEETING_DAYS_AHEAD):
            continue
        slug = (r.get("committee_url") or "").strip("/").split("/")[-1]
        if slug not in acr:
            try:
                c = get_json(na, f"{API}/committees/{slug}/?format=json")
                acr[slug] = {"a": (c.get("sessions") or [{}])[0].get("acronym") or "",
                             "n": (c.get("short_name") or c.get("name") or {}).get("en") or slug}
            except Exception:
                continue
        code = acr[slug]["a"]
        if code not in WATCH:
            continue
        m = dict(cache.get(r["url"]) or {})
        m.update({"url": r["url"], "date": r["date"], "code": code, "committee": acr[slug]["n"],
                  "num": r.get("number"), "in_camera": bool(r.get("in_camera"))})
        if not m.get("start") or now - m.get("detail_at", 0) > 6 * 3600:
            try:
                d = get_json(na, f"{API}{r['url']}?format=json")
                m.update({"start": (d.get("start_time") or "")[:5], "end": (d.get("end_time") or "")[:5],
                          "notice": d.get("notice_url") or "", "webcast": d.get("webcast_url") or "", "detail_at": int(now)})
            except Exception:
                pass
        m.setdefault("notice", f"https://www.ourcommons.ca/DocumentViewer/en/45-1/{code}/meeting-{r.get('number')}/notice")
        if m.get("notice") and now - m.get("agenda_at", 0) > AGENDA_REFRESH_H * 3600:
            try:
                subject, more = agenda_of(notice_lines(get_page(na, m["notice"])))
                if subject or not m.get("subject"):
                    m["subject"], m["agenda"] = subject, more
                m["agenda_at"] = int(now)
            except Exception:
                m.setdefault("subject", "")
                m.setdefault("agenda", [])
        topic = topic_for(na, m.get("subject", ""))   # the study's title only; witness lists match too much
        m["witness"] = next((ln for ln in m.get("agenda", []) if re.search(WITNESS_WORDS, ln, re.I)), "")
        m["assoc"] = topic["assoc"] if topic else WATCH[code]
        m["topic"] = topic["label"] if topic else ""
        m["first_seen"] = m.get("first_seen") or int(now)
        out.append(m)
    out.sort(key=lambda m: (m["date"], m.get("start") or "99"))
    return out, acr


# ---------------------------------------------------------------- mentions
def read_mentions(na, old, now):
    people = dict(old.get("people") or {})
    found = {m["link"]: m for m in old.get("mentions", [])}
    for src in na.FEEDS:
        if not str(src.get("id", "")).startswith("parl-"):
            continue
        try:
            items = list(na.parse(na.fetch(src["url"], tries=2)))
        except Exception:
            continue
        topic = next((t for t in na.TOPICS if t["key"] == src["topic"]), None)
        for it in items:
            link = it["link"].replace("http://", "https://")
            if link in found or not topic:
                continue
            m = re.match(r"^(.*?)\s*/\s*(.+)$", it["title"])
            ctx, who = (m.group(1).strip(), m.group(2).strip()) if m else ("", it["title"].strip())
            path = urllib.parse.urlparse(link).path.strip("/").split("/")
            committee = path[1].replace("-", " ").title() if path[:1] == ["committees"] and len(path) > 1 else ""
            found[link] = {"link": link, "who": who, "about": ctx, "quote": it["desc"][:420], "ts": int(it["ts"]),
                           "where": f"{committee} committee" if committee else "House of Commons",
                           "slug": re.sub(r"-\d+$", "", path[-1]) if path else "",
                           "term": src["parl"], "assoc": topic["assoc"], "topic": topic["label"],
                           "first_seen": int(now)}
    rows = sorted((m for m in found.values() if now - m["ts"] < MENTION_DAYS * 86400), key=lambda m: -m["ts"])[:80]
    looked = 0
    for m in rows:  # who the speaker is: "Conservative MP for Calgary Crowfoot"; blank for witnesses
        slug = m.get("slug")
        if slug and slug not in people and looked < 12:
            looked += 1
            try:
                p = get_json(na, f"{API}/politicians/{slug}/?format=json")
                cur = next((x for x in p.get("memberships", []) if not x.get("end_date")), None)
                people[slug] = ((cur or {}).get("label") or {}).get("en") or ""
            except Exception:
                people[slug] = ""
        m["role"] = people.get(slug, "")
    keep = {m.get("slug") for m in rows}
    return rows, {k: v for k, v in people.items() if k in keep}


# ---------------------------------------------------------------- notes and priorities
NOTE_SYSTEM = """You help a government-relations professional in Ottawa who follows two associations.
CADA: Canada's franchised new-car dealers. Their files: auto trade and tariffs, vehicle and emissions rules, EV \
policy, taxes on vehicles, consumer, financing, privacy and competition rules that bind dealers, auto plants.
HEAL: a coalition of national health organizations. Their files: federal health funding, the health workforce, \
pharmacare and drugs, mental health, health data and AI in health care, public health, medical assistance in \
dying, the Canada Health Act, seniors' and long-term care.

For each BILL return:
- "note": one or two plain sentences, 40 words at most, saying what the bill would do and its main points. Use only \
the title and official summary provided. If there is no summary, describe only what the title states; never invent \
provisions.
- "relevance" for the association it is filed under: 2 = directly affects that association's members or files; \
1 = related and worth knowing; 0 = not really relevant (it matched on a word).

For each committee MEETING return:
- "priority": 2 = important and applicable: a study squarely on one of the association's files, the association or \
its member or peer organizations testifying, a minister or senior officials appearing on those files, or \
clause-by-clause on a relevant bill. 1 = related to the sector but not pressing. 0 = routine or unrelated \
(committee business, election of a chair, a study on another subject).
- "assoc": CADA, HEAL, BOTH or NONE.
- "why": 16 words at most, concrete, saying why it matters (or what it is, for priority 0)."""
NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "bills": {"type": "array", "items": {
            "type": "object", "properties": {"n": {"type": "integer"}, "note": {"type": "string"},
                                             "relevance": {"type": "integer"}},
            "required": ["n", "note", "relevance"], "additionalProperties": False}},
        "meetings": {"type": "array", "items": {
            "type": "object", "properties": {"n": {"type": "integer"}, "priority": {"type": "integer"},
                                             "assoc": {"type": "string", "enum": ["CADA", "HEAL", "BOTH", "NONE"]},
                                             "why": {"type": "string"}},
            "required": ["n", "priority", "assoc", "why"], "additionalProperties": False}},
    },
    "required": ["bills", "meetings"], "additionalProperties": False,
}


def bill_summary(na, link_num, session="45-1") -> str:
    """The official SUMMARY printed at the front of a bill, or '' if it has none."""
    page = get_page(na, f"https://www.parl.ca/DocumentViewer/en/{session}/bill/{link_num}/first-reading")
    page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    at = text.rfind("SUMMARY")
    start = text.find("This enactment", at if at >= 0 else 0)
    if start < 0:
        return ""
    body = text[start:start + 2600]
    for stop in ("TABLE OF PROVISIONS", "Available on the House of Commons website", "RECOMMENDATION", "Preamble",
                 "His Majesty, by and with", "Her Majesty, by and with"):
        cut = body.find(stop)
        if cut > 80:
            body = body[:cut]
    return body.strip()[:1800]


def bill_sponsor(na, num, session="45-1") -> str:
    b = get_json(na, f"{API}/bills/{session}/{num}/?format=json")
    url = b.get("sponsor_politician_url") or ""
    if not url:
        return ""
    p = get_json(na, f"{API}{url}?format=json")
    cur = next((x for x in p.get("memberships", []) if not x.get("end_date")), None)
    role = ((cur or {}).get("label") or {}).get("en") or ""
    return (p.get("name") or "") + (f" ({role})" if role else "")


def ask_notes(bills, meetings):
    """Claude's notes for these bills and priorities for these meetings, or None."""
    if not os.environ.get("ANTHROPIC_API_KEY") or not (bills or meetings):
        return None
    import anthropic
    lines = ["BILLS:"] + [
        f"{n}. [{b['assoc']}] {b['num']} - {b['title']}" + (f" ({b['long']})" if b.get("long") else "")
        + f"\n   Official summary: {b.get('summary') or '(none available)'}" for n, b in enumerate(bills)]
    lines += ["", "MEETINGS:"] + [
        f"{n}. {m['committee']} committee ({m['code']}), {m['date']}: {m.get('subject') or '(agenda not posted)'}"
        + (" [in camera]" if m.get("in_camera") else "")
        + ("\n   Agenda and witnesses: " + "; ".join(m.get("agenda", [])[:18]) if m.get("agenda") else "")
        for n, m in enumerate(meetings)]
    model = (os.environ.get("CLAUDE_MODEL") or "").strip() or "claude-opus-5-5"
    resp = anthropic.Anthropic(timeout=90, max_retries=2).beta.messages.create(
        model=model, max_tokens=6000, system=NOTE_SYSTEM, messages=[{"role": "user", "content": "\n".join(lines)}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": NOTE_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def agenda_key(m) -> str:
    import hashlib
    return hashlib.sha1((m.get("subject", "") + "|" + "|".join(m.get("agenda", []))).encode()).hexdigest()[:12]


def annotate(na, data, now, asker=None):
    """Add a plain-language note to each bill and a priority to each meeting, remembering past answers."""
    notes = data.setdefault("notes", {})
    fetched = 0
    for b in data.get("bills", []):
        c = notes.setdefault("b:" + b["num"], {})
        if fetched < 8 and ("summary" not in c or (not c["summary"] and now - c.get("sum_at", 0) > 7 * 86400)):
            fetched += 1
            try:
                c["summary"] = bill_summary(na, b["num"])
            except Exception:
                c["summary"] = c.get("summary", "")
            c["sum_at"] = int(now)
            try:
                c["sponsor"] = bill_sponsor(na, b["num"])
            except Exception:
                c.setdefault("sponsor", "")
    need_b = [b for b in data.get("bills", [])
              if "summary" in notes["b:" + b["num"]] and (
                  not notes["b:" + b["num"]].get("note")
                  or (notes["b:" + b["num"]].get("from") == "title" and notes["b:" + b["num"]]["summary"]))][:12]
    for b in need_b:
        b["summary"] = notes["b:" + b["num"]]["summary"]
    need_m = [m for m in data.get("meetings", [])
              if notes.get("m:" + m["url"], {}).get("key") != agenda_key(m)][:15]
    try:
        out = (asker or ask_notes)(need_b, need_m)
    except Exception as e:
        print(f"parliament notes: {type(e).__name__}: {e}")
        out = None
    if out:
        for r in out.get("bills", []):
            if isinstance(r.get("n"), int) and 0 <= r["n"] < len(need_b):
                b = need_b[r["n"]]
                notes["b:" + b["num"]].update({
                    "note": re.sub(r"\s+", " ", str(r.get("note") or "")).strip()[:320],
                    "rel": max(0, min(2, int(r.get("relevance", 1)))),
                    "from": "summary" if b.get("summary") else "title"})
        for r in out.get("meetings", []):
            if isinstance(r.get("n"), int) and 0 <= r["n"] < len(need_m):
                m = need_m[r["n"]]
                notes["m:" + m["url"]] = {"key": agenda_key(m), "priority": max(0, min(2, int(r.get("priority", 0)))),
                                          "assoc": r.get("assoc", "NONE"),
                                          "why": re.sub(r"\s+", " ", str(r.get("why") or "")).strip()[:140], "at": int(now)}
    kept = []
    for b in data.get("bills", []):
        b.pop("summary", None)
        c = notes.get("b:" + b["num"], {})
        b["note"] = c.get("note", "")
        if c.get("sponsor") and not b.get("sponsor"):
            b["sponsor"] = c["sponsor"]
        if c.get("rel") == 0 and b["num"] not in TRACK_BILLS:
            continue   # matched on a word, but not really one of ours
        kept.append(b)
    data["bills"] = kept
    data["backlog"] = any(not b["note"] for b in kept)
    for m in data.get("meetings", []):
        c = notes.get("m:" + m["url"], {})
        if c.get("key") == agenda_key(m):
            m["priority"], m["why"] = c["priority"], c["why"]
            if c["assoc"] in ("CADA", "HEAL", "BOTH"):
                m["assoc"] = c["assoc"]
        else:   # no judgment yet: fall back to the word matches
            m["priority"], m["why"] = (2 if m.get("topic") or m.get("witness") else 0), ""
    live = {"b:" + b["num"] for b in data.get("bills", [])} | {"m:" + m["url"] for m in data.get("meetings", [])}
    data["notes"] = {k: v for k, v in notes.items() if k in live or (k.startswith("b:") and v.get("rel") == 0)}


# ---------------------------------------------------------------- all together
def update(news_path, now, na, force=False):
    path = os.path.join(os.path.dirname(news_path) or ".", "parliament.json")
    try:
        with open(path) as f:
            old = json.load(f)
    except Exception:
        old = {}
    # Catch up faster while some bills still lack their note.
    if not force and now - old.get("at", 0) < (10 if old.get("backlog") else REFRESH_MIN) * 60:
        return old
    new = dict(old)
    notes = []
    for name, job in (("bills", lambda: {"bills": read_bills(na)}),
                      ("meetings", lambda: dict(zip(("meetings", "acronyms"), read_meetings(na, old, now)))),
                      ("mentions", lambda: dict(zip(("mentions", "people"), read_mentions(na, old, now))))):
        try:
            got = job()
            new.update(got)
            notes.append(f"{len(got[name])} {name}")
        except Exception as e:  # keep the last good copy of that list
            notes.append(f"{name} failed ({type(e).__name__}: {e})")
    try:
        annotate(na, new, now)
    except Exception as e:
        notes.append(f"notes failed ({type(e).__name__}: {e})")
    new["at"] = int(now)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(new, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    print("parliament: " + ", ".join(notes))
    return new


def debug(na):
    """Run once into a scratch folder and print a short sample (used to check the sources from the runner)."""
    import tempfile
    d = update(os.path.join(tempfile.mkdtemp(), "news.json"), time.time(), na, force=True)
    for b in d.get("bills", [])[:14]:
        print(f"BILL {b['num']} [{b['assoc']}/{b['topic']}] step {b['done']}/7 | {b['status']} | {b['title'][:50]} | {b.get('sponsor', '')[:40]}\n      note: {b.get('note', '')}")
    for m in d.get("meetings", [])[:8]:
        print(f"MEET {m['date']} {m.get('start')} {m['code']} [{m['assoc']}/{m['topic']}] {m.get('subject', '')[:70]!r} +{len(m.get('agenda', []))} | P{m.get('priority')} {m.get('why', '')}")
    for m in d.get("mentions", [])[:4]:
        print(f"SAID {m['who']} | {m['role']} | {m['where']} | {m['about'][:40]} | {m['quote'][:90]}")
    return 0
