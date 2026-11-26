"""Streamlit dashboard for the GitHub Repository AI Analyzer."""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

import github_client as gh
from analyzer import AnalysisError, analyze

load_dotenv()
st.set_page_config(page_title="Repo AI Analyzer", page_icon="🔍", layout="wide")


def secret(name: str) -> str:
    try:
        value = st.secrets.get(name)
    except Exception:  # no secrets.toml present
        value = None
    return value or os.getenv(name, "")


# --------------------------------------------------------------------------- #
# Cached data access (avoids burning rate limit on reruns)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=600, show_spinner=False)
def load_repo(owner: str, repo: str, token: str) -> gh.RepoData:
    return gh.GitHubClient(token or None).fetch_repo(owner, repo)


@st.cache_data(ttl=3600, show_spinner=False)
def load_report(full_name: str, pushed_at: str, _data: gh.RepoData, api_key: str) -> dict:
    # full_name + pushed_at form the cache key; _data is not hashed.
    return analyze(_data, api_key)


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Settings")
    user_token = st.text_input(
        "GitHub token (optional)",
        type="password",
        help="Raises the limit from 60 to 5,000 requests/hour and allows private "
        "repos you have access to. Never stored.",
    )
    st.caption(
        "Without a token GitHub allows 60 requests/hour per IP. "
        "One analysis uses about 6-10 requests."
    )
    token = user_token or secret("GITHUB_TOKEN")
    groq_key = secret("GROQ_API_KEY")
    if not groq_key:
        st.error("GROQ_API_KEY is not configured on the server.")


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
st.title("🔍 GitHub Repository AI Analyzer")
st.write("Paste a GitHub repo link or `owner/repo` to get an AI-generated report.")

with st.form("repo_form"):
    col_in, col_btn = st.columns([5, 1])
    repo_input = col_in.text_input(
        "Repository",
        placeholder="https://github.com/streamlit/streamlit  or  streamlit/streamlit",
        label_visibility="collapsed",
    )
    submitted = col_btn.form_submit_button("Analyze", width="stretch")

if submitted:
    st.session_state["repo_input"] = repo_input

query = st.session_state.get("repo_input")
if not query:
    st.stop()


# --------------------------------------------------------------------------- #
# Fetch with friendly errors
# --------------------------------------------------------------------------- #
def fmt_reset(reset_at: datetime | None) -> str:
    if not reset_at:
        return ""
    minutes = max(0, int((reset_at - datetime.now(timezone.utc)).total_seconds() // 60))
    return f" Resets at {reset_at:%H:%M} UTC (in ~{minutes} min)."


try:
    owner, repo = gh.parse_repo(query)
except gh.InvalidRepoURL:
    st.error("Please enter a valid GitHub URL or owner/repo.")
    st.stop()

try:
    with st.spinner(f"Fetching {owner}/{repo} from GitHub…"):
        data = load_repo(owner, repo, token)
except gh.RepoNotFound:
    st.error(f"Repo **{owner}/{repo}** not found, or it's private. "
             "For a private repo, add a token with access to it in the sidebar.")
    st.stop()
except gh.RateLimited as exc:
    st.error("Rate limit reached. Add a GitHub token in the sidebar or try again later."
             + fmt_reset(exc.reset_at))
    st.stop()
except gh.BadToken:
    st.error("GitHub rejected the token. Check it hasn't expired, or clear the field.")
    st.stop()
except gh.NetworkError:
    st.error("Couldn't reach GitHub. Please retry.")
    st.stop()
except gh.GitHubError as exc:
    st.error(f"GitHub error: {exc}")
    st.stop()

meta = data.metadata


# --------------------------------------------------------------------------- #
# Header + stat cards
# --------------------------------------------------------------------------- #
st.subheader(f"[{data.full_name}]({meta['html_url']})")
if meta.get("description"):
    st.write(meta["description"])
if meta.get("topics"):
    st.markdown(" ".join(f"`{t}`" for t in meta["topics"]))
if meta.get("archived"):
    st.warning("This repository is archived (read-only).")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("⭐ Stars", f"{meta.get('stargazers_count', 0):,}")
c2.metric("🍴 Forks", f"{meta.get('forks_count', 0):,}")
c3.metric("🐛 Open issues", f"{meta.get('open_issues_count', 0):,}")
c4.metric("📜 License", (meta.get("license") or {}).get("spdx_id") or "None")
c5.metric("💻 Language", meta.get("language") or "n/a")

notes = []
if not data.readme:
    notes.append("no README found")
if data.tree_truncated:
    notes.append("file tree truncated by GitHub (very large repo)")
if data.rate_limit_remaining is not None:
    notes.append(f"{data.rate_limit_remaining} GitHub API requests left this hour")
st.caption(" · ".join(notes))

# Charts row
ch1, ch2 = st.columns(2)
with ch1:
    st.markdown("**Languages**")
    if data.languages:
        lang = pd.DataFrame(data.languages.items(), columns=["Language", "Bytes"])
        lang = lang.sort_values("Bytes", ascending=False)
        if len(lang) > 7:  # fold the long tail into "Other"
            other = lang.iloc[7:]["Bytes"].sum()
            lang = pd.concat(
                [lang.iloc[:7], pd.DataFrame([{"Language": "Other", "Bytes": other}])]
            )
        fig = px.pie(lang, names="Language", values="Bytes", hole=0.45)
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300)
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No language data.")

with ch2:
    st.markdown("**Files by top-level folder**")
    files = [p for p in data.tree if not p.endswith("/")]
    if files:
        tops = Counter(p.split("/")[0] if "/" in p else "(root)" for p in files)
        top = pd.DataFrame(tops.most_common(10), columns=["Folder", "Files"])
        fig = px.bar(top, x="Files", y="Folder", orientation="h")
        fig.update_layout(
            margin=dict(t=10, b=10, l=10, r=10), height=300,
            yaxis=dict(categoryorder="total ascending"),
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No files found (empty repository?).")


# --------------------------------------------------------------------------- #
# AI report
# --------------------------------------------------------------------------- #
report = None
if groq_key:
    try:
        with st.spinner("Generating AI analysis…"):
            report = load_report(data.full_name, meta.get("pushed_at", ""), data, groq_key)
    except AnalysisError as exc:
        st.error(f"AI analysis failed: {exc}")

tabs = st.tabs(["Summary", "Strengths & Architecture", "Recommendations",
                "Contribute", "File Structure"])

PRIORITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟢"}
DIFFICULTY_ICON = {"beginner": "🟢", "intermediate": "🟠", "advanced": "🔴"}

with tabs[0]:
    if report:
        st.markdown("### Executive summary")
        st.write(report["summary"])
        if report["tech_stack"]:
            st.markdown("### Tech stack")
            st.markdown(" ".join(f"`{t}`" for t in report["tech_stack"]))
    else:
        st.info("AI report unavailable.")

with tabs[1]:
    if report:
        st.markdown("### Strengths")
        for s in report["strengths"]:
            st.markdown(f"- ✅ {s}")
        st.markdown("### Architecture & code organization")
        st.write(report["architecture"])

with tabs[2]:
    if report:
        for i, r in enumerate(report["recommendations"], 1):
            icon = PRIORITY_ICON.get(r["priority"].lower(), "⚪")
            with st.container(border=True):
                st.markdown(f"**{i}. {r['title']}**  {icon} {r['priority'] or ''}"
                            f"  ·  `{r['category'] or 'general'}`")
                st.write(r["detail"])

with tabs[3]:
    if report:
        st.markdown("### Contribution opportunities")
        for c in report["contribution_opportunities"]:
            icon = DIFFICULTY_ICON.get(c["difficulty"].lower(), "⚪")
            with st.container(border=True):
                st.markdown(f"**{c['title']}**  {icon} {c['difficulty']}")
                st.write(c["detail"])
    st.markdown("### Open “good first issue” issues")
    if data.good_first_issues:
        for issue in data.good_first_issues:
            st.markdown(f"- [#{issue['number']} {issue['title']}]({issue['url']})")
    else:
        st.write("None currently labelled `good first issue`.")
    st.markdown(f"[Browse all issues →]({meta['html_url']}/issues)")

with tabs[4]:
    st.markdown(f"**{len(data.tree):,} paths** on `{meta.get('default_branch')}`")
    if data.package_files:
        st.markdown("**Package files found:** " +
                    ", ".join(f"`{n}`" for n in data.package_files))
    shown = data.tree[:500]
    st.code("\n".join(shown) or "(empty)", language="text")
    if len(data.tree) > len(shown):
        st.caption(f"Showing first {len(shown)} paths.")
    for name, content in data.package_files.items():
        with st.expander(name):
            st.code(content, language="toml" if name.endswith(".toml") else None)
