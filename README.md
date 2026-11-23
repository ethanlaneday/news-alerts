# News alerts: CADA + HEAL

Phone alerts and a news page for news that matters to **CADA** (Canadian Automobile Dealers
Association) and **HEAL** (Organizations for Health Action).

- **Alerts:** `.github/workflows/news-alerts.yml` runs `tools/news_alerts.py` every 5 minutes
  (started by cron-job.org; GitHub's own schedule is a backup). It checks Google News, Bing News,
  Canadian outlets, Parliament's bills and the Bank of Canada, rates each story 1–5 (Claude Haiku
  when `ANTHROPIC_API_KEY` is set, keyword rules otherwise) and pushes new ones to the ntfy app.
- **News page:** `index.html`, published by GitHub Pages at
  https://ethanlaneday.github.io/news-alerts/ — every rated story from the last week, sorted by
  importance. Its data (`news.json`) lives on the `news-data` branch, replaced each run.

Settings (Settings → Secrets and variables → Actions): secrets `NTFY_TOPIC`, `ANTHROPIC_API_KEY`;
variables `CLAUDE_MODEL` (e.g. `claude-haiku-5-5`), optional `MIN_LEVEL`, `MAX_AGE_HOURS`.
Topics, sources and scoring words are at the top of `tools/news_alerts.py`.
