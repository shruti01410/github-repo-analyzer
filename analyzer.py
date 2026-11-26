"""LLM analysis: turns RepoData into a structured JSON report via Groq."""

from __future__ import annotations

import json

from groq import Groq

from github_client import RepoData

MODEL = "llama-3.3-70b-versatile"
README_CHARS = 6000
TREE_PATHS = 200
PACKAGE_CHARS_TOTAL = 4000

SYSTEM_PROMPT = """You are a senior software engineer reviewing a GitHub repository \
for a developer who wants to understand and contribute to it. Base every claim on \
the supplied data; if something cannot be determined, say so instead of guessing. \
Respond with a single JSON object and nothing else."""

SCHEMA = """{
  "summary": "2-4 sentences: what the project is, who it is for, what problem it solves",
  "tech_stack": ["language / framework / key library", "..."],
  "strengths": ["specific strength grounded in the data", "..."],
  "architecture": "1 paragraph on architecture patterns and code organization seen in the file tree",
  "recommendations": [
    {"title": "short title", "category": "testing|documentation|ci_cd|modularity|security|dx|other",
     "priority": "high|medium|low", "detail": "concrete, actionable step and why it helps"}
  ],
  "contribution_opportunities": [
    {"title": "short title", "difficulty": "beginner|intermediate|advanced",
     "detail": "what to do and where in the repo to start"}
  ]
}"""


class AnalysisError(Exception):
    pass


def build_prompt(data: RepoData) -> str:
    m = data.metadata
    meta = {
        "full_name": data.full_name,
        "description": m.get("description"),
        "stars": m.get("stargazers_count"),
        "forks": m.get("forks_count"),
        "open_issues": m.get("open_issues_count"),
        "primary_language": m.get("language"),
        "license": (m.get("license") or {}).get("spdx_id"),
        "topics": m.get("topics", []),
        "default_branch": m.get("default_branch"),
        "created_at": m.get("created_at"),
        "last_push": m.get("pushed_at"),
        "archived": m.get("archived"),
        "languages_bytes": data.languages,
    }

    readme = (data.readme or "(no README found)")[:README_CHARS]

    tree = data.tree[:TREE_PATHS]
    tree_note = ""
    if len(data.tree) > TREE_PATHS or data.tree_truncated:
        tree_note = f"\n(showing first {len(tree)} of {len(data.tree)}+ paths)"

    pkg_parts, budget = [], PACKAGE_CHARS_TOTAL
    for name, content in data.package_files.items():
        if budget <= 0:
            break
        chunk = content[:budget]
        budget -= len(chunk)
        pkg_parts.append(f"--- {name} ---\n{chunk}")
    packages = "\n".join(pkg_parts) or "(no package/dependency files found)"

    issues = "\n".join(f"- #{i['number']} {i['title']}" for i in data.good_first_issues[:10])

    return f"""Analyze this repository.

## Metadata
{json.dumps(meta, indent=2)}

## README (truncated to {README_CHARS} chars)
{readme}

## File tree{tree_note}
{chr(10).join(tree) or "(empty)"}

## Package files
{packages}

## Open "good first issue" issues
{issues or "(none labelled)"}

Return JSON matching exactly this schema:
{SCHEMA}

Rules:
- At least 3 and at most 6 recommendations; each must be actionable and specific to this repo
  (e.g. reference real folders/files, or note a concrete absence such as no tests/ or no .github/workflows/).
- 3-5 contribution opportunities; mention the labelled issues above where relevant.
- 3-6 strengths.
"""


def analyze(data: RepoData, api_key: str) -> dict:
    if not api_key:
        raise AnalysisError("GROQ_API_KEY is not configured.")
    client = Groq(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(data)},
            ],
            temperature=0.3,
            max_tokens=2500,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # groq raises several SDK-specific types
        raise AnalysisError(f"LLM request failed: {exc}") from exc

    try:
        report = json.loads(resp.choices[0].message.content)
    except (json.JSONDecodeError, TypeError, IndexError) as exc:
        raise AnalysisError("The model returned invalid JSON.") from exc
    return normalize(report)


def normalize(report: dict) -> dict:
    """Coerce the model output into the shape the UI expects."""

    def as_list(value):
        if isinstance(value, list):
            return value
        return [value] if value else []

    def as_items(value, keys):
        items = []
        for v in as_list(value):
            if isinstance(v, str):
                v = {"title": v, "detail": ""}
            if isinstance(v, dict):
                items.append({k: str(v.get(k, "") or "") for k in keys})
        return items

    return {
        "summary": str(report.get("summary", "")),
        "tech_stack": [str(x) for x in as_list(report.get("tech_stack"))],
        "strengths": [str(x) for x in as_list(report.get("strengths"))],
        "architecture": str(report.get("architecture", "")),
        "recommendations": as_items(
            report.get("recommendations"), ("title", "category", "priority", "detail")
        ),
        "contribution_opportunities": as_items(
            report.get("contribution_opportunities"), ("title", "difficulty", "detail")
        ),
    }
