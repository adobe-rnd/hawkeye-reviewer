#!/usr/bin/env python3
"""Claude PR Reviewer via Bedrock — posts Copilot-style reviews on pull requests.

Uses only the Python standard library (no pip install needed).
"""

import base64
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"

CLAUDE_AVATAR = "https://github.com/anthropics.png?size=36"

STATUS_CONTEXT = "Claude PR Review"

SEVERITY_ICONS = {
    "critical": ":rotating_light:",
    "warning": ":warning:",
    "suggestion": ":bulb:",
    "nitpick": ":mag:",
}

SEVERITY_LABELS = {
    "critical": "Critical",
    "warning": "Warning",
    "suggestion": "Suggestion",
    "nitpick": "Nitpick",
}


# ---------------------------------------------------------------------------
# Model name detection
# ---------------------------------------------------------------------------


def get_model_name(api_url: str) -> str:
    """Extract a friendly model name from the Bedrock endpoint URL."""
    match = re.search(r"/model/([^/]+)/", api_url)
    if not match:
        return "Claude"

    model_id = match.group(1).lower()

    friendly_names = {
        "claude-sonnet-4-5": "Claude Sonnet 4.5",
        "claude-opus-4": "Claude Opus 4",
        "claude-3-7-sonnet": "Claude 3.7 Sonnet",
        "claude-3-5-sonnet": "Claude 3.5 Sonnet",
        "claude-3-5-haiku": "Claude 3.5 Haiku",
        "claude-3-opus": "Claude 3 Opus",
        "claude-3-sonnet": "Claude 3 Sonnet",
        "claude-3-haiku": "Claude 3 Haiku",
    }
    for key, name in friendly_names.items():
        if key in model_id:
            return name

    cleaned = model_id.replace("anthropic.", "")
    cleaned = re.sub(r"-\d{8}-v\d+.*$", "", cleaned)
    return cleaned.replace("-", " ").title() or "Claude"


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no requests dependency)
# ---------------------------------------------------------------------------


def _request(method: str, url: str, headers: dict, data: bytes | None = None, timeout: int = 60) -> dict:
    """Make an HTTP request and return {"status": int, "body": Any}."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "body": json.loads(body) if body else {}}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return {"status": exc.code, "body": body}


def github_get(url: str, token: str, params: dict | None = None) -> dict:
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    result = _request("GET", url, headers)
    if result["status"] >= 400:
        raise RuntimeError(f"GET {url} → {result['status']}: {result['body']}")
    return result["body"]


def github_post(url: str, token: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8")
    return _request("POST", url, headers, data=data)


def github_patch(url: str, token: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8")
    return _request("PATCH", url, headers, data=data)


def http_post(url: str, headers: dict, payload: dict, timeout: int = 180) -> dict:
    data = json.dumps(payload).encode("utf-8")
    return _request("POST", url, headers, data=data, timeout=timeout)


# ---------------------------------------------------------------------------
# Commit Status API
# ---------------------------------------------------------------------------


def set_commit_status(owner: str, repo: str, sha: str, state: str, description: str, token: str) -> None:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/statuses/{sha}"
    result = github_post(url, token, {"state": state, "context": STATUS_CONTEXT, "description": description})
    if result["status"] >= 400:
        raise RuntimeError(f"Failed to set commit status: {result['body']}")
    print(f"Commit status set to '{state}' on {sha[:8]}.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Placeholder comment (post → edit in-place)
# ---------------------------------------------------------------------------

PLACEHOLDER_BODY = (
    f'<h2><img src="{CLAUDE_AVATAR}" width="18" height="18" align="absmiddle"> '
    "Reviewing your PR...</h2>\n\n"
    ":hourglass_flowing_sand: Claude is analyzing your changes. "
    "A detailed review with inline comments will appear here shortly."
)


def post_placeholder_comment(owner: str, repo: str, pr_number: int, token: str) -> int:
    """Post a placeholder comment and return the comment ID."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    result = github_post(url, token, {"body": PLACEHOLDER_BODY})
    if result["status"] >= 400:
        raise RuntimeError(f"Failed to post placeholder: {result['body']}")
    print("Placeholder comment posted.", file=sys.stderr)
    return result["body"]["id"]


def edit_comment(owner: str, repo: str, comment_id: int, body: str, token: str) -> None:
    """Edit an existing issue comment in-place."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}"
    result = github_patch(url, token, {"body": body})
    if result["status"] >= 400:
        raise RuntimeError(f"Failed to edit comment: {result['body']}")
    print("Placeholder comment updated with review.", file=sys.stderr)


# ---------------------------------------------------------------------------
# GitHub data fetching
# ---------------------------------------------------------------------------


def get_pr_info(owner: str, repo: str, pr_number: int, token: str) -> dict:
    return github_get(f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}", token)


def get_changed_files(owner: str, repo: str, pr_number: int, token: str) -> list[dict]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    files: list[dict] = []
    page = 1
    while True:
        batch = github_get(url, token, params={"page": str(page), "per_page": "100"})
        if not batch:
            break
        files.extend(batch)
        page += 1
    return files


def get_file_content(owner: str, repo: str, ref: str, path: str, token: str) -> str:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    try:
        data = github_get(url, token, params={"ref": ref})
    except RuntimeError:
        return ""
    if isinstance(data, dict) and data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return ""


def get_diff_lines(patch: str) -> set[int]:
    """Parse a unified diff patch and return line numbers (new file side) that live inside a hunk."""
    if not patch:
        return set()
    lines: set[int] = set()
    current_line = 0
    for raw in patch.splitlines():
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.add(current_line)
        else:
            lines.add(current_line)
        current_line += 1
    return lines


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

REVIEW_PROMPT = textwrap.dedent("""\
    You are an expert code reviewer. Review the pull request below and respond
    with a single JSON object — no markdown fences, no commentary outside the JSON.

    ## Pull Request
    **Title:** {title}
    **Description:** {description}

    ## Response schema

    ```
    {{
      "summary": {{
        "overview": "1-3 sentence description of what this PR does",
        "changes": ["bullet 1", "bullet 2"],
        "files": [
          {{"path": "relative/file.py", "description": "What changed in this file"}}
        ]
      }},
      "comments": [
        {{
          "path": "relative/file.py",
          "line": 42,
          "severity": "critical | warning | suggestion | nitpick",
          "message": "Explanation of the issue and how to fix it.",
          "suggestion": "optional — the replacement line(s) that fix the issue"
        }}
      ]
    }}
    ```

    ## Rules
    - Only comment on lines that appear as ADDED (+) in each file's DIFF section.
    - "suggestion" must be the exact replacement code (no line-number prefix, no
      explanation) so it can be used in a GitHub "suggested change" block.
      Multi-line suggestions are fine (separate lines with \\n).
    - Focus on: correctness bugs, security issues, missing error handling,
      performance problems, and important improvements.
    - Do NOT comment on minor style/formatting preferences.
    - Keep each "message" to 1-3 sentences — concise and actionable.
    - If everything looks good, return {{"summary": {{...}}, "comments": []}}.

    ## Changed files

    {files_text}
""")


def build_prompt(
    pr_info: dict,
    files: list[dict],
    owner: str,
    repo: str,
    head_sha: str,
    token: str,
) -> str:
    included_files: list[str] = []
    total_chars = 0
    max_chars = 80_000

    for f in files:
        if f.get("status") == "removed":
            continue
        path = f["filename"]
        if f.get("changes", 0) > 800:
            continue

        content = get_file_content(owner, repo, head_sha, path, token)
        if not content:
            continue

        numbered = "\n".join(
            f"{i}: {line}" for i, line in enumerate(content.splitlines(), start=1)
        )
        patch = f.get("patch", "")
        diff_section = f"\nDIFF:\n{patch}\n" if patch else ""
        block = f"FILE: {path} (status: {f.get('status', 'modified')})\n{numbered}{diff_section}\n\n"

        if total_chars + len(block) > max_chars:
            break
        included_files.append(block)
        total_chars += len(block)

    files_text = "\n".join(included_files) if included_files else "(No reviewable file content.)"

    return REVIEW_PROMPT.format(
        title=pr_info.get("title", ""),
        description=(pr_info.get("body") or "(no description)")[:2000],
        files_text=files_text,
    )


# ---------------------------------------------------------------------------
# Claude / Bedrock
# ---------------------------------------------------------------------------


def call_claude(prompt: str, api_url: str, api_token: str) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
    }
    payload = {
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
    }
    result = http_post(api_url, headers, payload, timeout=180)
    if result["status"] >= 400:
        raise RuntimeError(f"Claude API error {result['status']}: {result['body']}")
    data = result["body"]

    try:
        contents = (data.get("output") or {}).get("message", {}).get("content", [])
        for c in contents:
            if isinstance(c, dict) and c.get("text"):
                return c["text"]
    except Exception:
        pass

    return json.dumps(data)


def parse_response(text: str) -> dict[str, Any]:
    """Parse Claude's JSON, stripping any accidental markdown fences."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        print(f"JSON parse error: {exc}", file=sys.stderr)
        print(f"Raw text (first 500 chars): {cleaned[:500]}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_summary_comment(summary: dict, comments: list[dict], model_name: str = "Claude") -> str:
    parts: list[str] = []

    logo = f'<img src="{CLAUDE_AVATAR}" width="18" height="18" align="absmiddle">'

    overview = summary.get("overview", "")
    if overview:
        parts.append(f"<h2>{logo} Pull request overview</h2>\n\n{overview}")

    changes = summary.get("changes", [])
    if changes:
        bullets = "\n".join(f"- {c}" for c in changes)
        parts.append(f"**Changes:**\n{bullets}")

    files = summary.get("files", [])
    if files:
        rows = "\n".join(
            f"| `{f.get('path', '')}` | {f.get('description', '')} |" for f in files
        )
        table = "| File | Description |\n|------|-------------|\n" + rows
        parts.append(f"### Reviewed changes\n\n{table}")

    if comments:
        counts: dict[str, int] = {}
        for c in comments:
            sev = c.get("severity", "suggestion")
            counts[sev] = counts.get(sev, 0) + 1
        breakdown = ", ".join(
            f"{SEVERITY_ICONS.get(s, '')} {count} {s}" for s, count in counts.items()
        )
        parts.append(
            f"\n---\n{len(comments)} inline comment{'s' if len(comments) != 1 else ''} "
            f"posted ({breakdown})."
        )
    else:
        parts.append(
            "\n---\n:white_check_mark: No issues found — looks good!"
        )

    footer_logo = f'<img src="{CLAUDE_AVATAR}" width="13" height="13" align="absmiddle">'
    parts.append(
        f"<sub>{footer_logo} Reviewed by **{model_name}** (Anthropic) via Amazon Bedrock "
        "| Type `/claude-review` in a comment to re-review after new commits</sub>"
    )

    return "\n\n".join(parts)


def format_inline_body(comment: dict) -> str:
    sev = comment.get("severity", "suggestion")
    icon = SEVERITY_ICONS.get(sev, ":bulb:")
    label = SEVERITY_LABELS.get(sev, "Suggestion")
    message = comment.get("message", "")

    body = f"{icon} **{label}**\n\n{message}"

    suggestion = comment.get("suggestion")
    if suggestion:
        body += f"\n\n```suggestion\n{suggestion}\n```"

    return body


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def post_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    summary_body: str,
    inline_comments: list[dict],
    valid_lines: dict[str, set[int]],
    token: str,
    placeholder_comment_id: int | None = None,
) -> None:
    if placeholder_comment_id:
        edit_comment(owner, repo, placeholder_comment_id, summary_body, token)
    else:
        issue_url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        result = github_post(issue_url, token, {"body": summary_body})
        if result["status"] >= 400:
            raise RuntimeError(f"Failed to post summary: {result['body']}")
        print("Summary comment posted.", file=sys.stderr)

    if not inline_comments:
        return

    review_comments: list[dict] = []
    for c in inline_comments:
        path = c.get("path", "")
        line = c.get("line")
        if not path or not isinstance(line, int):
            continue
        if path in valid_lines and line in valid_lines[path]:
            review_comments.append(
                {"path": path, "line": line, "side": "RIGHT", "body": format_inline_body(c)}
            )
        else:
            print(f"  Skipped (not in diff): {path}:{line}", file=sys.stderr)

    if not review_comments:
        return

    review_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    payload = {"commit_id": commit_sha, "event": "COMMENT", "comments": review_comments}
    result = github_post(review_url, token, payload)

    if result["status"] == 422:
        print("Batch review returned 422 — retrying individually...", file=sys.stderr)
        posted, skipped = 0, 0
        for rc in review_comments:
            single = {"commit_id": commit_sha, "event": "COMMENT", "comments": [rc]}
            r = github_post(review_url, token, single)
            if r["status"] < 400:
                posted += 1
            else:
                skipped += 1
                print(f"  Skipped {rc['path']}:{rc['line']} ({r['status']})", file=sys.stderr)
        print(f"Individual posting: {posted} posted, {skipped} skipped.", file=sys.stderr)
    elif result["status"] >= 400:
        raise RuntimeError(f"Failed to post review: {result['body']}")
    else:
        print(f"{len(review_comments)} inline comments posted.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 4:
        print(
            "Usage: claude_pr_review.py <owner> <repo> <pr_number> [--invalidate]",
            file=sys.stderr,
        )
        sys.exit(1)

    owner, repo, pr_str = sys.argv[1:4]
    pr_number = int(pr_str)
    invalidate = "--invalidate" in sys.argv

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        print("Missing required environment variable: GITHUB_TOKEN", file=sys.stderr)
        sys.exit(1)

    pr_info = get_pr_info(owner, repo, pr_number, github_token)
    head_sha = pr_info["head"]["sha"]

    if invalidate:
        print(f"Invalidating review status for PR #{pr_number}...", file=sys.stderr)
        set_commit_status(
            owner, repo, head_sha, "pending",
            "New commits pushed — type /claude-review to re-review.",
            github_token,
        )
        return

    api_url = os.environ.get("CLAUDE_API_URL")
    api_token = os.environ.get("CLAUDE_API_TOKEN")

    missing = []
    if not api_url:
        missing.append("CLAUDE_API_URL")
    if not api_token:
        missing.append("CLAUDE_API_TOKEN")
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    model_name = get_model_name(api_url)
    print(f"Reviewing PR #{pr_number} on {owner}/{repo} with {model_name}...", file=sys.stderr)

    set_commit_status(owner, repo, head_sha, "pending", f"{model_name} is reviewing this PR...", github_token)
    placeholder_id = post_placeholder_comment(owner, repo, pr_number, github_token)

    files = get_changed_files(owner, repo, pr_number, github_token)
    print(f"  {len(files)} changed file(s).", file=sys.stderr)

    valid_lines: dict[str, set[int]] = {}
    for f in files:
        valid_lines[f["filename"]] = get_diff_lines(f.get("patch", ""))

    prompt = build_prompt(pr_info, files, owner, repo, head_sha, github_token)

    print("  Calling Claude...", file=sys.stderr)
    claude_text = call_claude(prompt, api_url, api_token)

    response = parse_response(claude_text)
    if not response:
        print("  Could not parse Claude response; posting raw text as summary.", file=sys.stderr)
        logo = f'<img src="{CLAUDE_AVATAR}" width="18" height="18" align="absmiddle">'
        footer_logo = f'<img src="{CLAUDE_AVATAR}" width="13" height="13" align="absmiddle">'
        fallback_body = (
            f"<h2>{logo} AI PR Review</h2>\n\n"
            "Claude returned a response that could not be parsed as structured JSON.\n\n"
            f"<details><summary>Raw response</summary>\n\n```\n{claude_text[:4000]}\n```\n</details>\n\n"
            f"<sub>{footer_logo} Reviewed by **{model_name}** (Anthropic) via Amazon Bedrock</sub>"
        )
        edit_comment(owner, repo, placeholder_id, fallback_body, github_token)
        set_commit_status(owner, repo, head_sha, "success", "Review complete", github_token)
        return

    summary = response.get("summary", {})
    comments = [
        c
        for c in response.get("comments", [])
        if isinstance(c, dict) and c.get("path") and isinstance(c.get("line"), int) and c.get("message")
    ]

    summary_body = format_summary_comment(summary, comments, model_name)
    post_review(owner, repo, pr_number, head_sha, summary_body, comments, valid_lines, github_token, placeholder_id)
    set_commit_status(owner, repo, head_sha, "success", "Review complete", github_token)

    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
