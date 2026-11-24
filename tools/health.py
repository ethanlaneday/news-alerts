"""Watchdog: is the news job still running? Run on its own schedule by health.yml.

The news job publishes fresh data every run, so the age of the news-data branch says whether it is
alive. If it has gone quiet this restarts it, and if it stays quiet it sends one phone alert
(repeated every six hours, never overnight), then a note when it recovers.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

RESTART_AFTER_MIN = 20
ALERT_AFTER_MIN = 45
REPEAT_HOURS = 6
QUIET = (22, 8)


def decide(age_min, state, now, hour):
    """What to do for data this many minutes old. Returns (actions, new state); pure, so it can be tested."""
    actions, state = [], dict(state)
    quiet = hour >= QUIET[0] or hour < QUIET[1]
    if age_min >= RESTART_AFTER_MIN:
        actions.append("restart")
    if age_min >= ALERT_AFTER_MIN:
        if not quiet and now - state.get("alerted", 0) > REPEAT_HOURS * 3600:
            actions.append("alert")
            state["alerted"] = now
    elif age_min < RESTART_AFTER_MIN and state.get("alerted"):
        actions.append("recovered")
        state["alerted"] = 0
    return actions, state


def api(url, token, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "news-alerts-health"}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if body else {}


def notify(title, message, tags):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    body = {"topic": topic, "title": title, "message": message, "priority": 4, "tags": tags}
    req = urllib.request.Request(os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/") + "/",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def main() -> int:
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    path = os.environ.get("HEALTH_STATE", ".health-state/state.json")
    try:
        with open(path) as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = time.time()
    commit = api(f"https://api.github.com/repos/{repo}/commits/news-data", token)
    when = datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00")).timestamp()
    age = (now - when) / 60
    hour = datetime.now(ZoneInfo("America/Toronto")).hour
    actions, state = decide(age, state, now, hour)
    print(f"news data is {age:.0f} min old -> {actions or 'fine'}")
    if "restart" in actions:
        try:
            api(f"https://api.github.com/repos/{repo}/actions/workflows/news-alerts.yml/dispatches", token, {"ref": "main"})
        except Exception as e:
            print(f"restart failed: {e}", file=sys.stderr)
    if "alert" in actions:
        notify("News alerts have stopped", f"No news check has completed for {age:.0f} minutes. A restart was tried "
               "automatically. If this repeats, check the Actions tab on GitHub and the cron-job.org schedule.", ["rotating_light"])
    if "recovered" in actions:
        notify("News alerts are running again", "Checks have resumed.", ["white_check_mark"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
