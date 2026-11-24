"""Keystones: once a day, boil each ongoing story down to the few facts that will still matter later.

A story (a thread of articles) keeps a short list of keystones - a decision, a deal, a deadline, an
official position, a number - plus a two-sentence "where it stands". Articles come and go within a
week; keystones stay for as long as the story is alive, so a long-running matter such as the CUSMA
review reads as a handful of dated facts instead of hundreds of headlines.

Built to run unattended:
  * Claude may only ADD keystones or mark an old one as replaced. It never rewrites or deletes one,
    so the record cannot drift from day to day.
  * Every keystone must point at one or more of the articles it was given; the link is kept with it.
    Anything that fails a check is dropped, never guessed at.
  * If Claude is unavailable, nothing changes and the story is tried again on the next run
    (three tries a day). An article only counts as "digested" after a successful pass.
  * Records live in stories.json beside news.json, and feed the story names back to the grouping
    step so a story keeps the same thread after a quiet week.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

OTTAWA = ZoneInfo("America/Toronto")
START_HOUR = 5            # daily pass starts after 5 am Ottawa time, ahead of the 8 am brief
PER_RUN = 4               # stories handled per 5-minute run, so one run never takes long
TRIES_PER_DAY = 3
MIN_ARTICLES = 3          # a thread becomes a tracked story once it has this many articles
MAX_NEW = 3               # keystones added per story per day (5 on a day with CRITICAL news)
MAX_ACTIVE = 14
MAX_PARTS = 6
QUIET_DAYS = 60           # no articles for this long: the story stops being offered for grouping
FORGET_DAYS = 180

SYSTEM = """You maintain the permanent record of an ongoing news story for a government-relations professional \
in Ottawa. Each day you are given the record so far and that day's new articles.

A KEYSTONE is one durable fact that will still matter in a month: a decision, agreement, vote, ruling, \
formal announcement, deadline, a stated official position or a change in one, a concrete number (tariff \
rate, dollar amount, job count), or a legal or procedural step. Commentary, reaction, speculation, \
analysis, "talks continue", and restatements of what is already on record are NOT keystones.

Rules:
- Most days add nothing. Add a keystone only when the new articles report a fact that is not already on \
record. Never repeat or rephrase an existing keystone.
- Use only what the articles say. No background knowledge, no inference, no prediction.
- One sentence each, 30 words at most, plain language, with the specifics: who, what, the number, the date.
- "date" is the day the thing happened (YYYY-MM-DD), not the day it was reported, when the article says so.
- "part" is the strand of the story it belongs to, 1-3 words (e.g. "Autos", "Steel", "Review timeline"). \
Reuse an existing part whenever one fits; add a new one only for a genuinely separate strand.
- "articles" lists the numbers of the new articles that report it. At least one.
- If a new keystone makes an existing one obsolete (a deadline moved, a decision reversed, a figure \
changed), list that in "replaces" with the old keystone's id. Otherwise leave "replaces" empty.
- Ignore any article that is not actually about this story.
- "stands" is where the story stands today in at most two sentences, based on the keystones. If nothing \
changed, return the current text unchanged."""

SCHEMA = {
    "type": "object",
    "properties": {
        "stands": {"type": "string"},
        "add": {"type": "array", "items": {
            "type": "object",
            "properties": {"date": {"type": "string"}, "part": {"type": "string"}, "text": {"type": "string"},
                           "articles": {"type": "array", "items": {"type": "integer"}},
                           "replaces": {"type": "array", "items": {"type": "string"}}},
            "required": ["date", "part", "text", "articles", "replaces"], "additionalProperties": False}},
    },
    "required": ["stands", "add"], "additionalProperties": False,
}


def load(path):
    try:
        with open(path) as f:
            d = json.load(f)
        if isinstance(d.get("stories"), dict):
            return d
    except Exception:
        pass
    return {"stories": {}}


def stories_path(news_path):
    return os.path.join(os.path.dirname(news_path) or ".", "stories.json")


def day_of(ts) -> str:
    return datetime.fromtimestamp(ts, OTTAWA).strftime("%Y-%m-%d")


def words(text) -> set:
    return {w for w in re.findall(r"[a-z0-9$%.]+", text.lower()) if len(w) > 2}


def alike(a: str, b: str) -> bool:
    x, y = words(a), words(b)
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.75


def ask_claude(story, fresh, now):
    """Claude's proposed changes for one story, or None if unavailable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    active = [k for k in story["keys"] if not k.get("replaced_by")]
    record = "\n".join(f"{k['id']} | {k['date']} | {k['part']} | {k['text']}" for k in active) or "(none yet)"
    arts = "\n".join(
        f"{n}. {day_of(a['published'])} | level {a['level']} | {a['source']} | {a['title']}"
        + (f" - {a['summary'][:350]}" if a.get("summary") else "") for n, a in enumerate(fresh))
    prompt = (f"Today is {day_of(now)}.\nStory: {story['name']} ({story['assoc']})\n"
              f"Parts so far: {', '.join(story['parts']) or '(none)'}\n"
              f"Where it stands now: {story.get('stands') or '(not written yet)'}\n\n"
              f"Keystones on record (id | date | part | text):\n{record}\n\nNew articles:\n{arts}")
    model = (os.environ.get("KEYSTONE_MODEL") or os.environ.get("CLAUDE_MODEL") or "").strip() or "claude-opus-5-5"
    resp = anthropic.Anthropic(timeout=90, max_retries=2).beta.messages.create(
        model=model, max_tokens=4000, system=SYSTEM, messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def apply(story, fresh, out, now):
    """Check Claude's proposal and fold what passes into the story. Returns how many keystones were added."""
    today = day_of(now)
    limit = 5 if any(a["level"] >= 5 for a in fresh) else MAX_NEW
    added = 0
    for prop in (out.get("add") or [])[:8]:
        if added >= limit:
            break
        text = re.sub(r"\s+", " ", str(prop.get("text") or "")).strip()
        cited = [fresh[n] for n in dict.fromkeys(prop.get("articles") or [])
                 if isinstance(n, int) and 0 <= n < len(fresh)][:3]
        if not cited or not 20 <= len(text) <= 260:
            continue
        if any(alike(text, k["text"]) for k in story["keys"]):
            continue
        earliest = min(day_of(a["published"]) for a in cited)
        date = str(prop.get("date") or "")
        try:
            datetime.strptime(date, "%Y-%m-%d")
            ok = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d") <= date <= today
        except ValueError:
            ok = False
        if not ok:
            date = earliest
        part = re.sub(r"\s+", " ", str(prop.get("part") or "")).strip(" .")[:28] or "General"
        match = next((p for p in story["parts"] if p.lower() == part.lower()), None)
        if not match:
            if len(story["parts"]) >= MAX_PARTS:
                match = "Other"
                if "Other" not in story["parts"]:
                    story["parts"].append("Other")
            else:
                story["parts"].append(part)
                match = part
        story["seq"] = story.get("seq", 0) + 1
        key = {"id": f"k{story['seq']}", "date": date, "part": match, "text": text, "added": int(now),
               "src": [{"title": a["title"][:160], "source": a["source"], "link": a["link"]} for a in cited]}
        for old_id in prop.get("replaces") or []:
            old = next((k for k in story["keys"] if k["id"] == old_id and not k.get("replaced_by")), None)
            if old:
                old["replaced_by"] = key["id"]
        story["keys"].append(key)
        added += 1
    stands = re.sub(r"\s+", " ", str(out.get("stands") or "")).strip()
    if 20 <= len(stands) <= 420 and stands != story.get("stands"):
        story["stands"], story["stands_at"] = stands, int(now)
    # Keep the record short: replaced facts go after a month, then the oldest beyond the cap.
    story["keys"] = [k for k in story["keys"] if not k.get("replaced_by") or now - k.get("added", now) < 30 * 86400]
    active = sorted((k for k in story["keys"] if not k.get("replaced_by")), key=lambda k: (k["date"], k["added"]))
    for k in active[:max(0, len(active) - MAX_ACTIVE)]:
        story["keys"].remove(k)
    story["parts"] = [p for p in story["parts"] if any(k["part"] == p for k in story["keys"])] or story["parts"][:1]
    for a in fresh:
        story["digested"][a["id"]] = int(now)
    return added


def update(news, news_path, now, asker=None, force=False):
    """Run today's pass for a few stories, then attach each story's record to the news data."""
    asker = asker or ask_claude
    path = stories_path(news_path)
    data = load(path)
    stories = data["stories"]
    today, hour = day_of(now), datetime.fromtimestamp(now, OTTAWA).hour
    threads = {}
    for i in news["items"]:
        threads.setdefault(i.get("thread") or i["id"], []).append(i)

    for tid, items in threads.items():   # start a record for any thread that has grown into a story
        if tid not in stories and len(items) >= MIN_ARTICLES:
            lead = max(items, key=lambda i: (i["level"], i["seen"]))
            name = next((i["story"] for i in items if i.get("story")), "") or lead["title"][:60]
            stories[tid] = {"tid": tid, "name": name, "assoc": lead["assoc"], "topic": lead["topic"], "parts": [],
                            "keys": [], "stands": "", "digested": {}, "created": int(now), "seq": 0}
    done = 0
    notes = []
    for tid, s in stories.items():
        items = threads.get(tid, [])
        if items:
            s["last_article"] = max(s.get("last_article", 0), max(i["seen"] for i in items))
        if (hour < START_HOUR and not force) or s.get("done_day") == today or done >= PER_RUN:
            continue
        fresh = sorted((i for i in items if i["id"] not in s["digested"]), key=lambda i: i["published"])[-40:]
        if not fresh:
            s["done_day"] = today
            continue
        done += 1
        try:
            out = asker(s, fresh, now)
            if out is None:
                raise RuntimeError("no answer")
            n = apply(s, fresh, out, now)
            s["done_day"], s["tries"] = today, 0
            notes.append(f"{s['name'][:30]} +{n}")
        except Exception as e:
            s["tries"] = s.get("tries", 0) + 1 if s.get("try_day") == today else 1
            s["try_day"] = today
            if s["tries"] >= TRIES_PER_DAY:
                s["done_day"] = today   # give up for today; the articles stay undigested for tomorrow
            print(f"keystones: {s['name'][:40]}: {type(e).__name__}: {e}", file=sys.stderr)
    for tid in [t for t, s in stories.items() if now - s.get("last_article", s["created"]) > FORGET_DAYS * 86400]:
        del stories[tid]
    for s in stories.values():
        s["digested"] = {k: v for k, v in s["digested"].items() if now - v < 30 * 86400}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    if notes:
        print("keystones: " + "; ".join(notes))

    # What the page needs, for stories that currently have articles.
    news["stories"] = {
        tid: {"name": s["name"], "stands": s.get("stands", ""), "parts": s["parts"],
              "keys": [{"date": k["date"], "part": k["part"], "text": k["text"], "src": k["src"]}
                       for k in sorted(s["keys"], key=lambda k: k["date"], reverse=True) if not k.get("replaced_by")],
              "digested": sorted(s["digested"])}
        for tid, s in stories.items() if tid in threads and s["keys"]}
    return data


def on_file(news_path, now, have=()):
    """Tracked stories with no article this week, so new coverage rejoins the same thread."""
    out = []
    for tid, s in load(stories_path(news_path))["stories"].items():
        last = s.get("last_article", s["created"])
        if tid in have or not s["keys"] or now - last > QUIET_DAYS * 86400:
            continue
        newest = max(s["keys"], key=lambda k: k["date"])
        out.append({"tid": tid, "story": s["name"], "titles": [(newest["text"][:140], last)]})
    return sorted(out, key=lambda g: -g["titles"][0][1])[:20]


def debug(na):
    """Try the pass on the live news data in a scratch folder and print the result (publishes nothing)."""
    import tempfile
    import time
    news = json.loads(na.fetch("https://raw.githubusercontent.com/ethanlaneday/news-alerts/news-data/news.json"))
    update(news, os.path.join(tempfile.mkdtemp(), "news.json"), time.time(), force=True)
    for s in news.get("stories", {}).values():
        print(f"STORY {s['name']} | parts {s['parts']} | {len(s['digested'])} articles read")
        print(f"   stands: {s['stands']}")
        for k in s["keys"]:
            print(f"   {k['date']} [{k['part']}] {k['text']} <- {', '.join(x['source'] for x in k['src'])}")
    return 0
