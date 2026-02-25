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
import urllib.parse
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"

CLAUDE_AVATAR = "https://github.com/anthropics.png?size=36"

STATUS_CONTEXT = "Claude Bedrock PR Review"

SEVERITY_ICONS = {
    "critical": "\U0001f6a8",
    "warning": "\u26a0\ufe0f",
    "suggestion": "\U0001f4a1",
    "design": "\U0001f4d0",
    "nitpick": "\U0001f50d",
}

SEVERITY_LABELS = {
    "critical": "Critical",
    "warning": "Warning",
    "suggestion": "Suggestion",
    "design": "Design",
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

    # Strip common prefixes so matching is clean
    stripped = re.sub(r"^(us\.|eu\.|ap\.)?anthropic\.", "", model_id)

    # Ordered most-specific first: longer version numbers before shorter ones
    # so "opus-4-6" matches before "opus-4"
    families = ["opus", "sonnet", "haiku"]
    versions = ["4-6", "4-5", "4"]
    friendly_names: list[tuple[str, str]] = []

    for ver in versions:
        display_ver = ver.replace("-", ".")
        for family in families:
            friendly_names.append(
                (f"claude-{family}-{ver}", f"Claude {family.title()} {display_ver}")
            )

    # Claude 3.x naming uses a different pattern: claude-3-{minor}-{family}
    legacy = [
        ("claude-3-7-sonnet", "Claude 3.7 Sonnet"),
        ("claude-3-5-sonnet", "Claude 3.5 Sonnet"),
        ("claude-3-5-haiku", "Claude 3.5 Haiku"),
        ("claude-3-opus", "Claude 3 Opus"),
        ("claude-3-sonnet", "Claude 3 Sonnet"),
        ("claude-3-haiku", "Claude 3 Haiku"),
    ]
    friendly_names.extend(legacy)

    for key, name in friendly_names:
        if key in stripped:
            return name

    # Fallback: clean up the raw model ID into a readable name
    cleaned = re.sub(r"-\d{8}-v\d+.*$", "", stripped)
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
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = body
            return {"status": resp.status, "body": parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(body) if body else body
        except json.JSONDecodeError:
            parsed = body
        return {"status": exc.code, "body": parsed}


def github_get(url: str, token: str, params: dict | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
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
    "\u23f3 Claude is analyzing your changes. "
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


REPO_CONTEXT_FILES = [
    # Multi-language version managers
    ".tool-versions",
    # Python
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    ".python-version",
    "requirements.txt",
    "Pipfile",
    # JavaScript / TypeScript
    "package.json",
    "tsconfig.json",
    ".nvmrc",
    ".node-version",
    # Java / Kotlin
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    # Scala
    "build.sbt",
    "project/build.properties",
    # Go
    "go.mod",
    # Rust
    "Cargo.toml",
    # Containers & infrastructure
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    # General
    ".editorconfig",
]

REPO_CONTEXT_MAX_PER_FILE = 2048
REPO_CONTEXT_MAX_TOTAL = 12_000

REPO_DOCS_FILES = [
    "README.md",
    "CONTRIBUTING.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    ".cursorrules",
    ".cursor/rules/review.md",
    ".cursor/rules/code-style.md",
    ".github/CODEOWNERS",
    ".github/pull_request_template.md",
]

REPO_DOCS_MAX_PER_FILE = 3000
REPO_DOCS_MAX_TOTAL = 8000

GUIDELINES_PATHS = [
    ".github/claude-review.md",
    ".claude-review.md",
]

GUIDELINES_MAX_CHARS = 4000


def get_repo_context(owner: str, repo: str, ref: str, token: str) -> str:
    """Fetch well-known config files from the repo to give Claude project context."""
    blocks: list[str] = []
    total_chars = 0

    for path in REPO_CONTEXT_FILES:
        content = get_file_content(owner, repo, ref, path, token)
        if not content:
            continue
        truncated = content[:REPO_CONTEXT_MAX_PER_FILE]
        if len(content) > REPO_CONTEXT_MAX_PER_FILE:
            truncated += "\n... (truncated)"
        block = f"### {path}\n```\n{truncated}\n```"
        if total_chars + len(block) > REPO_CONTEXT_MAX_TOTAL:
            break
        blocks.append(block)
        total_chars += len(block)
        print(f"  repo context: included {path} ({len(content)} chars)", file=sys.stderr)

    return "\n\n".join(blocks)


def get_repo_docs(owner: str, repo: str, ref: str, token: str) -> str:
    """Fetch project documentation and convention files for additional review context."""
    blocks: list[str] = []
    total_chars = 0

    for path in REPO_DOCS_FILES:
        content = get_file_content(owner, repo, ref, path, token)
        if not content:
            continue
        truncated = content[:REPO_DOCS_MAX_PER_FILE]
        if len(content) > REPO_DOCS_MAX_PER_FILE:
            truncated += "\n... (truncated)"
        block = f"### {path}\n```\n{truncated}\n```"
        if total_chars + len(block) > REPO_DOCS_MAX_TOTAL:
            break
        blocks.append(block)
        total_chars += len(block)
        print(f"  repo docs: included {path} ({len(content)} chars)", file=sys.stderr)

    return "\n\n".join(blocks)


def get_review_guidelines(owner: str, repo: str, ref: str, token: str) -> str:
    """Fetch optional review guidelines from the repo."""
    for path in GUIDELINES_PATHS:
        content = get_file_content(owner, repo, ref, path, token)
        if content:
            print(f"  review guidelines: loaded from {path}", file=sys.stderr)
            if len(content) > GUIDELINES_MAX_CHARS:
                return content[:GUIDELINES_MAX_CHARS] + "\n... (truncated)"
            return content
    return ""


RELATED_BUILD_FILES = [
    "vite.config.ts",
    "vite.config.js",
    "vite.config.mjs",
    "webpack.config.js",
    "webpack.config.ts",
    "webpack.config.mjs",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "rollup.config.js",
    "rollup.config.mjs",
    "jest.config.js",
    "jest.config.ts",
    "jest.config.mjs",
    "vitest.config.ts",
    "vitest.config.js",
]

RELATED_CONTEXT_MAX_PER_FILE = 3000
RELATED_CONTEXT_MAX_TOTAL = 15_000


def _infer_test_paths(filepath: str) -> list[str]:
    """Given a source file path, return candidate test file paths to look for."""
    if not filepath or filepath.startswith("."):
        return []
    # Skip files that are already tests
    base = os.path.basename(filepath)
    if any(marker in base for marker in [".test.", ".spec.", "__test__"]):
        return []
    # Skip non-source files
    name, ext = os.path.splitext(base)
    if ext not in (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".mts"):
        return []

    dirpart = os.path.dirname(filepath)
    candidates = [
        os.path.join(dirpart, f"{name}.test{ext}"),
        os.path.join(dirpart, f"{name}.spec{ext}"),
        os.path.join(dirpart, "__tests__", f"{name}{ext}"),
        os.path.join(dirpart, "__tests__", f"{name}.test{ext}"),
    ]
    if ext == ".py":
        candidates.append(os.path.join(dirpart, f"test_{name}{ext}"))
        if dirpart:
            parent = os.path.dirname(dirpart)
            leaf = os.path.basename(dirpart)
            candidates.append(os.path.join(parent, "tests", leaf, f"test_{name}{ext}"))
            candidates.append(os.path.join(parent, "tests", f"test_{name}{ext}"))
        else:
            candidates.append(os.path.join("tests", f"test_{name}{ext}"))
    return candidates


def _infer_build_config_paths(changed_files: list[dict]) -> list[str]:
    """Identify build config files that might be relevant based on changed file locations."""
    dirs_seen: set[str] = set()
    for f in changed_files:
        path = f.get("filename", "")
        parts = path.split("/")
        # Collect root and first-level directories that might have their own configs
        if len(parts) > 1:
            dirs_seen.add(parts[0])

    paths: list[str] = []
    # Root-level build configs
    paths.extend(RELATED_BUILD_FILES)
    # Subproject build configs (e.g. dashboard/vite.config.ts)
    for d in sorted(dirs_seen):
        for cfg in RELATED_BUILD_FILES:
            paths.append(f"{d}/{cfg}")
    return paths


def get_related_context(
    changed_files: list[dict],
    owner: str,
    repo: str,
    ref: str,
    token: str,
) -> str:
    """Fetch test files and build configs related to the changed files."""
    blocks: list[str] = []
    total_chars = 0
    fetched: set[str] = set()
    changed_paths = {f["filename"] for f in changed_files}

    # Collect all candidate paths (test files + build configs)
    candidates: list[tuple[str, str]] = []  # (path, reason)

    for f in changed_files:
        for tp in _infer_test_paths(f["filename"]):
            if tp not in changed_paths:
                candidates.append((tp, f"test for {f['filename']}"))

    for bp in _infer_build_config_paths(changed_files):
        if bp not in changed_paths:
            candidates.append((bp, "build config"))

    for path, reason in candidates:
        if path in fetched or total_chars >= RELATED_CONTEXT_MAX_TOTAL:
            continue
        content = get_file_content(owner, repo, ref, path, token)
        if not content:
            continue
        fetched.add(path)
        truncated = content[:RELATED_CONTEXT_MAX_PER_FILE]
        if len(content) > RELATED_CONTEXT_MAX_PER_FILE:
            truncated += "\n... (truncated)"
        block = f"### {path}\n_Reason: {reason}_\n```\n{truncated}\n```"
        if total_chars + len(block) > RELATED_CONTEXT_MAX_TOTAL:
            break
        blocks.append(block)
        total_chars += len(block)
        print(f"  related context: included {path} ({len(content)} chars, {reason})", file=sys.stderr)

    return "\n\n".join(blocks)


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

    {repo_context_section}

    {repo_docs_section}

    {guidelines_section}

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
          "severity": "critical | warning | suggestion | design | nitpick",
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
    - Do NOT comment on minor style/formatting preferences.
    - Keep each "message" to 1-3 sentences — concise and actionable.
    - Use the repository context (language versions, dependencies, config) to
      calibrate your review. Do not suggest patterns or syntax incompatible with
      the project's declared runtime, frameworks, or dependencies.
    - If repository guidelines are provided, follow them. They take precedence
      over your default review preferences.
    - Be thorough and critical. A pull request with more than a handful of
      additions almost always has at least one issue worth flagging. It is better
      to surface a potential concern that turns out to be fine than to miss a real
      bug, security hole, or broken contract.
    - Think about cross-file implications: does a change in one file break
      assumptions, tests, or contracts in another? Consider build/bundler configs,
      test mocks, API schemas, security definitions, and deployment settings.
    - If related context files (tests, configs) are provided below, use them to
      catch issues like broken test mocks, incompatible build settings, or
      mismatched API contracts.
    - Only return an empty "comments" array after you have carefully examined
      every changed file and genuinely found nothing actionable.

    ## What to look for

    Review like a senior engineer with 10+ years of experience. Work through each
    of these categories systematically for every changed file:

    ### Correctness and edge cases
    - Null/nil/None dereferences, missing nil checks
    - Empty collections passed where non-empty is assumed
    - Off-by-one errors in loops, slices, and ranges
    - Integer overflow, underflow, or truncation on cast
    - Unicode and encoding issues in string processing
    - Boundary values: zero, negative, max-int, empty string

    ### Security
    - Hardcoded secrets, API keys, passwords, or tokens
    - SQL injection (string concatenation in queries)
    - XSS (unsanitized user input rendered in HTML)
    - Path traversal (user input in file paths without sanitization)
    - SSRF (user-controlled URLs in server-side requests)
    - Insecure deserialization of untrusted data
    - Missing input validation or overly permissive allow-lists
    - Overly broad CORS, IAM, or file permissions

    ### Concurrency and thread safety
    - Race conditions on shared mutable state
    - Missing locks, synchronization, or atomic operations
    - Deadlock potential (lock ordering issues)
    - Non-atomic check-then-act patterns (TOCTOU)
    - Unsafe publication of objects between threads

    ### Resource management
    - Unclosed connections, file handles, streams, or sockets
    - Missing try-with-resources, context managers, or defer statements
    - Potential memory leaks (unbounded caches, growing collections, listeners
      not removed)
    - Unbounded queues or buffers that can cause OOM under load

    ### Error handling
    - Swallowed exceptions (empty catch/except blocks)
    - Generic catch-all handlers that hide root causes
    - Error messages that lack context for debugging in production
    - Missing cleanup or rollback in error/failure paths
    - Panics or unchecked exceptions that could crash the process

    ### Test coverage
    - New logic or branches added without corresponding test cases
    - Tests that assert on the wrong thing or don't actually verify behavior
    - Missing edge case tests for the boundary conditions listed above
    - Flaky test patterns (time-dependent, order-dependent, non-deterministic)

    ### API design and contracts
    - Breaking changes to public interfaces or method signatures
    - Missing input validation at API boundaries
    - Inconsistent return types, error formats, or status codes
    - Missing or incorrect documentation on public APIs

    ### Design improvements (use "design" severity)
    - Algorithm and data structure choices (e.g. DFS vs BFS, hash map vs sorted
      set, quadratic vs linear approach)
    - More suitable libraries or built-in functions that simplify the code
    - Language-specific optimizations (e.g. Java streams/virtual threads, Python
      generators/slots, Go channels, Scala tail recursion, Rust zero-cost
      abstractions)
    - Architectural decisions (e.g. Fargate vs Lambda, queues vs synchronous
      calls, caching layers)
    - Scalability concerns and cost optimization opportunities

    ### Cross-file and integration concerns
    - Imports/requires that reference paths outside the project or build root
    - Existing test files that mock or stub behavior being changed (broken mocks)
    - OpenAPI/Swagger specs: global security schemes inherited by new endpoints,
      missing security overrides on public routes
    - Build tool configs (Vite, Webpack, Rollup, etc.) that may not support new
      import patterns or file locations
    - Infrastructure-as-code (CDK, Terraform, CloudFormation) that contradicts
      the API spec or application config
    - Database migrations or schema changes that are inconsistent with ORM models

    {related_context_section}

    ## Changed files

    {files_text}
""")


def _build_file_block(path: str, status: str, content: str, patch: str) -> str:
    """Build a full file block with numbered source and diff."""
    numbered = "\n".join(
        f"{i}: {line}" for i, line in enumerate(content.splitlines(), start=1)
    )
    diff_section = f"\nDIFF:\n{patch}\n" if patch else ""
    return f"FILE: {path} (status: {status})\n{numbered}{diff_section}\n\n"


def _build_diff_only_block(path: str, status: str, patch: str) -> str:
    """Build a lighter block containing only the diff (no full source)."""
    return f"FILE: {path} (status: {status}) [diff only — file too large for full content]\nDIFF:\n{patch}\n\n"


def build_prompt(
    pr_info: dict,
    files: list[dict],
    owner: str,
    repo: str,
    head_sha: str,
    token: str,
    related_context: str = "",
) -> str:
    included_files: list[str] = []
    skipped_files: list[str] = []
    total_chars = 0
    max_chars = 180_000

    for f in files:
        if f.get("status") == "removed":
            continue
        path = f["filename"]
        patch = f.get("patch", "")
        status = f.get("status", "modified")
        changes = f.get("changes", 0)

        if changes > 800:
            if patch:
                block = _build_diff_only_block(path, status, patch)
                if total_chars + len(block) <= max_chars:
                    included_files.append(block)
                    total_chars += len(block)
                    print(f"  {path}: included diff only ({changes} changes, too large for full content)", file=sys.stderr)
                    continue
            skipped_files.append(f"{path} ({changes} changes — exceeded budget even for diff)")
            print(f"  {path}: skipped ({changes} changes, exceeded budget)", file=sys.stderr)
            continue

        content = get_file_content(owner, repo, head_sha, path, token)
        if not content:
            if patch:
                block = _build_diff_only_block(path, status, patch)
                if total_chars + len(block) <= max_chars:
                    included_files.append(block)
                    total_chars += len(block)
                    print(f"  {path}: included diff only (could not fetch content)", file=sys.stderr)
                    continue
            skipped_files.append(f"{path} (could not fetch content)")
            continue

        block = _build_file_block(path, status, content, patch)

        if total_chars + len(block) > max_chars:
            if patch:
                fallback = _build_diff_only_block(path, status, patch)
                if total_chars + len(fallback) <= max_chars:
                    included_files.append(fallback)
                    total_chars += len(fallback)
                    print(f"  {path}: included diff only (full content exceeded budget)", file=sys.stderr)
                    continue
            skipped_files.append(f"{path} (exceeded budget)")
            print(f"  {path}: skipped (exceeded budget)", file=sys.stderr)
            continue

        included_files.append(block)
        total_chars += len(block)

    if skipped_files:
        print(f"  {len(skipped_files)} file(s) skipped: {'; '.join(skipped_files)}", file=sys.stderr)

    files_text = "\n".join(included_files) if included_files else "(No reviewable file content.)"

    repo_context = get_repo_context(owner, repo, head_sha, token)
    repo_context_section = f"## Repository context\n\n{repo_context}" if repo_context else ""

    repo_docs = get_repo_docs(owner, repo, head_sha, token)
    repo_docs_section = f"## Project documentation\n\n{repo_docs}" if repo_docs else ""

    guidelines = get_review_guidelines(owner, repo, head_sha, token)
    guidelines_section = f"## Repository guidelines\n\n{guidelines}" if guidelines else ""

    related_context_section = (
        f"## Related context files (not part of this PR)\n\n"
        f"These files are NOT being changed but may be affected by or relevant to the changes above.\n\n"
        f"{related_context}"
    ) if related_context else ""

    return REVIEW_PROMPT.format(
        title=pr_info.get("title", ""),
        description=(pr_info.get("body") or "(no description)")[:2000],
        repo_context_section=repo_context_section,
        repo_docs_section=repo_docs_section,
        guidelines_section=guidelines_section,
        related_context_section=related_context_section,
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
            "\n---\n\u2705 No issues found — looks good!"
        )

    footer_logo = f'<img src="{CLAUDE_AVATAR}" width="13" height="13" align="absmiddle">'
    parts.append(
        f"<sub>{footer_logo} Reviewed by **{model_name}** (Anthropic) via Amazon Bedrock "
        "| Type `/claude-review` in a comment to re-review after new commits</sub>"
    )

    return "\n\n".join(parts)


def format_inline_body(comment: dict) -> str:
    sev = comment.get("severity", "suggestion")
    icon = SEVERITY_ICONS.get(sev, "\U0001f4a1")
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


def get_existing_review_comments(owner: str, repo: str, pr_number: int, token: str) -> set[tuple[str, int, str]]:
    """Fetch existing review comments and return a set of (path, line, severity_label) tuples."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
    existing: set[tuple[str, int, str]] = set()
    page = 1
    while True:
        try:
            batch = github_get(url, token, params={"page": str(page), "per_page": "100"})
        except RuntimeError:
            break
        if not batch:
            break
        for c in batch:
            path = c.get("path", "")
            line = c.get("line") or c.get("original_line")
            body = c.get("body", "")
            if not path or not isinstance(line, int):
                continue
            for label in SEVERITY_LABELS.values():
                if f"**{label}**" in body:
                    existing.add((path, line, label))
                    break
        page += 1
    return existing


def filter_comments(
    inline_comments: list[dict],
    valid_lines: dict[str, set[int]],
    existing_comments: set[tuple[str, int, str]],
) -> list[dict]:
    """Filter out comments that are outside the diff or already posted."""
    filtered: list[dict] = []
    deduped = 0
    for c in inline_comments:
        path = c.get("path", "")
        line = c.get("line")
        if not path or not isinstance(line, int):
            continue
        if path not in valid_lines or line not in valid_lines[path]:
            print(f"  Skipped (not in diff): {path}:{line}", file=sys.stderr)
            continue
        sev = c.get("severity", "suggestion")
        label = SEVERITY_LABELS.get(sev, "Suggestion")
        if (path, line, label) in existing_comments:
            deduped += 1
            print(f"  Skipped (duplicate): {path}:{line} [{label}]", file=sys.stderr)
            continue
        filtered.append(c)

    if deduped:
        print(f"  {deduped} duplicate comment(s) skipped.", file=sys.stderr)
    return filtered


def post_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    summary_body: str,
    inline_comments: list[dict],
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
        review_comments.append(
            {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": format_inline_body(c)}
        )

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


RETRY_PROMPT = textwrap.dedent("""\
    You are an expert code reviewer performing a second-pass review of a pull
    request. Your first review found zero issues, but the PR has {total_additions}
    additions across {num_files} files — this warrants a closer look.

    Re-examine the diffs below with a more critical eye. Pay special attention to:

    1. **Cross-file implications** — do changes in one file break assumptions,
       mocks, or contracts in other files (including files not in this PR)?
    2. **Security configuration** — OpenAPI/Swagger global security schemes
       inherited by new endpoints, missing auth overrides on public routes,
       overly broad IAM or CORS permissions.
    3. **Build and bundler compatibility** — imports referencing paths outside
       the project root, new file patterns unsupported by the build tool config.
    4. **Test coverage gaps** — existing tests that mock or stub behavior being
       changed, new logic paths without test coverage.
    5. **API contract consistency** — schema references, error response formats,
       status codes, and documentation accuracy.

    Respond with a single JSON object (no markdown fences):

    ```
    {{
      "comments": [
        {{
          "path": "relative/file.py",
          "line": 42,
          "severity": "critical | warning | suggestion | design | nitpick",
          "message": "Explanation of the issue and how to fix it.",
          "suggestion": "optional replacement code"
        }}
      ]
    }}
    ```

    Only comment on ADDED (+) lines. If you still genuinely find nothing after
    careful re-examination, return {{"comments": []}}.

    ## Pull Request
    **Title:** {title}
    **Description:** {description}

    {related_context_section}

    ## Changed files (diffs only)

    {diffs_text}
""")

MIN_ADDITIONS_FOR_RETRY = 150


def build_retry_prompt(
    pr_info: dict,
    files: list[dict],
    related_context: str = "",
) -> str:
    """Build a shorter, diff-only prompt for the second-pass review."""
    diffs: list[str] = []
    total_additions = sum(f.get("additions", 0) for f in files)

    for f in files:
        if f.get("status") == "removed":
            continue
        patch = f.get("patch", "")
        if not patch:
            continue
        path = f["filename"]
        status = f.get("status", "modified")
        diffs.append(f"FILE: {path} (status: {status})\nDIFF:\n{patch}\n")

    diffs_text = "\n".join(diffs) if diffs else "(no diffs)"

    related_context_section = (
        f"## Related context files (not part of this PR)\n\n{related_context}"
    ) if related_context else ""

    return RETRY_PROMPT.format(
        total_additions=total_additions,
        num_files=len([f for f in files if f.get("status") != "removed"]),
        title=pr_info.get("title", ""),
        description=(pr_info.get("body") or "(no description)")[:2000],
        related_context_section=related_context_section,
        diffs_text=diffs_text,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 4:
        print(
            "Usage: claude_pr_review.py <owner> <repo> <pr_number>",
            file=sys.stderr,
        )
        sys.exit(1)

    owner, repo, pr_str = sys.argv[1:4]
    pr_number = int(pr_str)

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        print("Missing required environment variable: GITHUB_TOKEN", file=sys.stderr)
        sys.exit(1)

    pr_info = get_pr_info(owner, repo, pr_number, github_token)
    head_sha = pr_info["head"]["sha"]

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

    placeholder_id_str = os.environ.get("PLACEHOLDER_COMMENT_ID", "")
    placeholder_id = int(placeholder_id_str) if placeholder_id_str else None
    if not placeholder_id:
        placeholder_id = post_placeholder_comment(owner, repo, pr_number, github_token)

    logo = f'<img src="{CLAUDE_AVATAR}" width="18" height="18" align="absmiddle">'
    footer_logo = f'<img src="{CLAUDE_AVATAR}" width="13" height="13" align="absmiddle">'

    try:
        files = get_changed_files(owner, repo, pr_number, github_token)
        print(f"  {len(files)} changed file(s).", file=sys.stderr)

        valid_lines: dict[str, set[int]] = {}
        for f in files:
            valid_lines[f["filename"]] = get_diff_lines(f.get("patch", ""))

        related_context = get_related_context(files, owner, repo, head_sha, github_token)
        prompt = build_prompt(pr_info, files, owner, repo, head_sha, github_token, related_context)

        print("  Calling Claude...", file=sys.stderr)
        claude_text = call_claude(prompt, api_url, api_token)

        response = parse_response(claude_text)
        if not response:
            print("  Could not parse Claude response; posting raw text as summary.", file=sys.stderr)
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
        all_returned = response.get("comments", [])
        raw_comments = [
            c
            for c in all_returned
            if isinstance(c, dict) and c.get("path") and isinstance(c.get("line"), int) and c.get("message")
        ]
        print(
            f"  Claude returned {len(all_returned)} comment(s), "
            f"{len(raw_comments)} valid after schema check.",
            file=sys.stderr,
        )

        existing = get_existing_review_comments(owner, repo, pr_number, github_token)
        if existing:
            print(f"  Found {len(existing)} existing review comment(s) for dedup.", file=sys.stderr)
        comments = filter_comments(raw_comments, valid_lines, existing)
        print(f"  {len(comments)} comment(s) survived filtering.", file=sys.stderr)

        # Second-pass review if first pass found nothing on a large PR
        total_additions = sum(f.get("additions", 0) for f in files)
        if not comments and total_additions >= MIN_ADDITIONS_FOR_RETRY:
            print(
                f"  Zero comments on {total_additions} additions — running second-pass review...",
                file=sys.stderr,
            )
            retry_prompt = build_retry_prompt(pr_info, files, related_context)
            retry_text = call_claude(retry_prompt, api_url, api_token)
            retry_response = parse_response(retry_text)
            if retry_response:
                retry_all = retry_response.get("comments", [])
                retry_raw = [
                    c
                    for c in retry_all
                    if isinstance(c, dict) and c.get("path") and isinstance(c.get("line"), int) and c.get("message")
                ]
                print(
                    f"  Second pass returned {len(retry_all)} comment(s), "
                    f"{len(retry_raw)} valid.",
                    file=sys.stderr,
                )
                retry_comments = filter_comments(retry_raw, valid_lines, existing)
                print(f"  {len(retry_comments)} second-pass comment(s) survived filtering.", file=sys.stderr)
                comments.extend(retry_comments)

        summary_body = format_summary_comment(summary, comments, model_name)
        post_review(owner, repo, pr_number, head_sha, summary_body, comments, github_token, placeholder_id)
        set_commit_status(owner, repo, head_sha, "success", "Review complete", github_token)

        print("Done.", file=sys.stderr)

    except Exception as exc:
        print(f"  Review failed: {exc}", file=sys.stderr)
        error_body = (
            f"<h2>{logo} AI PR Review</h2>\n\n"
            f"\u274c Review failed: `{type(exc).__name__}: {exc}`\n\n"
            "This may be a transient issue. Type `/claude-review` in a comment to retry.\n\n"
            f"<sub>{footer_logo} Reviewed by **{model_name}** (Anthropic) via Amazon Bedrock</sub>"
        )
        try:
            edit_comment(owner, repo, placeholder_id, error_body, github_token)
            set_commit_status(owner, repo, head_sha, "error", "Review failed — type /claude-review to retry", github_token)
        except Exception as cleanup_exc:
            print(f"  Cleanup also failed: {cleanup_exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
