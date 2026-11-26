# GitHub Repository AI Analyzer

Paste a GitHub repo link (`https://github.com/owner/repo` or `owner/repo`) and get an
AI-generated report: what the project does, its tech stack, strengths, architecture,
at least 3 actionable recommendations, and where to start contributing.

**Live demo:** _add your Streamlit Cloud URL here_

## Features
- Stat cards: stars, forks, open issues, license, primary language
- Charts: language breakdown (donut) and files per top-level folder (bar)
- Tabs: Summary · Strengths & Architecture · Recommendations · Contribute · File Structure
- Live "good first issue" list pulled from the repo
- Optional GitHub token for higher rate limits and private repos
- Friendly errors for invalid URLs, missing/private repos, rate limits, bad tokens and network failures

## Project layout
```
app.py            Streamlit dashboard (UI + error messages)
github_client.py  All GitHub REST API calls, URL parsing, error types
analyzer.py       Prompt building, truncation, Groq call, JSON normalisation
tests/            Unit tests (parser, error handling, truncation, normalisation)
```

## API flow
1. `parse_repo()` extracts `owner`/`repo` with a regex (also accepts `.git` suffixes and deep links like `/tree/main/src`).
2. `GitHubClient.fetch_repo()` calls:

| Data | Endpoint |
|---|---|
| Metadata | `GET /repos/{owner}/{repo}` |
| README | `GET /repos/{owner}/{repo}/readme` (base64-decoded) |
| File tree | `GET /repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1` |
| Languages | `GET /repos/{owner}/{repo}/languages` |
| Package files | `GET /repos/{owner}/{repo}/contents/{requirements.txt, pyproject.toml, package.json, ...}` |
| Good first issues | `GET /repos/{owner}/{repo}/issues?labels=good first issue` |

   Package files are only requested if the tree shows they exist, which saves requests.
3. `analyzer.build_prompt()` packs the data into a prompt, truncated to stay inside the
   model's limits: README → first 6,000 chars, tree → first 200 paths, package files →
   4,000 chars total.
4. Groq (`openai/gpt-oss-120b` by default, overridable via `GROQ_MODEL`) is called in JSON mode. `normalize()` coerces the
   response into a fixed schema so the UI never breaks on slightly malformed output.
5. Results are cached (`st.cache_data`): GitHub data for 10 minutes, AI reports per
   `repo + last push` for 1 hour, so reruns don't burn rate limit or tokens.

## Error handling
| Situation | Behaviour |
|---|---|
| Input doesn't parse | "Please enter a valid GitHub URL or owner/repo." |
| 404 on metadata | "Repo not found, or it's private." (suggests adding a token) |
| 403/429 with `X-RateLimit-Remaining: 0` | "Rate limit reached…" plus the reset time from `X-RateLimit-Reset` |
| 401 | "GitHub rejected the token." |
| Timeout / connection error | "Couldn't reach GitHub. Please retry." |
| Missing README / package file / empty repo (409) | Skipped quietly; the report still runs |
| LLM failure or invalid JSON | Error shown; GitHub stats and charts still display |

## Rate limits
Unauthenticated: **60 requests/hour** per IP. With a personal access token:
**5,000 requests/hour**. One analysis uses roughly 6–10 requests. The remaining quota is
shown under the stat cards. Users can paste a token in the sidebar (it is only kept
in memory for the session); a server-side `GITHUB_TOKEN` secret is used as a fallback.

## Run locally
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # then fill in GROQ_API_KEY
streamlit run app.py
```
Tests: `pip install pytest && pytest -q`

## Deploy (Streamlit Community Cloud)
1. Push this repo to GitHub.
2. On share.streamlit.io → New app → pick the repo, main file `app.py`.
3. In **Settings → Secrets** add:
   ```toml
   GROQ_API_KEY = "gsk_..."
   GITHUB_TOKEN = ""   # optional
   ```
No secrets are committed: `.env` and `.streamlit/secrets.toml` are git-ignored.
