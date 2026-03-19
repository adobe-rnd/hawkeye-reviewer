# HawkEye Reviewer

AI-powered pull request reviews using Claude (Anthropic) via Amazon Bedrock. Provides senior-engineer-level review comments with inline suggestions, design feedback, and automatic repo context awareness — all from a single Python script with zero dependencies.

<p align="center">
  <img src="diagrams/01_system_architecture.png" alt="System Architecture">
</p>

## Features

- **Automatic reviews** on every PR (opened, reopened, ready for review)
- **On-demand reviews** via `@hawkeye review` comment or the "Re-request review" button in the Reviewers sidebar
- **Appears as a reviewer** — HawkEye requests itself as a reviewer when a PR is opened, showing up in the Reviewers sidebar like Copilot
- **Map-reduce pipeline** — handles large PRs (8+ files or 1500+ changes) with parallel batch reviews and cross-file consolidation
- **Smart token optimization** — 30-40% token savings via expanded diff context, structural signatures, and intelligent truncation
- **Full repository awareness** — directory tree, sibling files, and imported modules so Claude understands your project structure, coding patterns, and internal APIs
- **Linter-aware suggestions** — fetches your project's linter/formatter configs (64+ patterns across 10+ languages) so every `suggestion` block respects your rules
- **Custom guidelines** — optional `.github/hawkeye-review.md` for repo-specific review instructions
- **Inline code suggestions** with GitHub's native suggestion blocks (one-click apply)
- **5 severity levels** — critical, warning, suggestion, design, nitpick — each with distinct icons
- **Senior-level review checklist** — security, concurrency, edge cases, resource management, test coverage, API contracts, design, and dead code
- **Comment deduplication** — skips duplicate comments on re-review so you don't get the same feedback twice
- **Instant feedback** — posts a placeholder comment immediately while Claude analyzes
- **Version label and AI disclaimer** — every comment shows the reviewer version and an AI-generated content notice
- **Merge gate** — sets a commit status (`HawkEye Review`) that can be required in branch protection rules
- **Draft PR aware** — skips draft PRs to avoid wasting API calls
- **Zero dependencies** — uses only Python's standard library (no `pip install`)

## How it works

The bot runs as a **standalone webhook server** (`webhook_server.py`) that receives GitHub webhooks directly and triggers the review script as a subprocess.

1. GitHub sends a webhook event (PR opened, `@hawkeye review` comment) to the server
2. The server validates the HMAC signature, generates a GitHub App installation token, posts a placeholder comment, and queues the review
3. `hawkeye_pr_review.py` assembles the prompt, calls Claude via Bedrock, and posts inline review comments

## Setup

### 1. GitHub App

Create a GitHub App (**GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**):

- **Name:** Any unique name (e.g. "HawkEye Reviewer")
- **Homepage URL:** Your repo URL
- **Webhook:** Active ✓, URL: `https://your-server/webhook`, Secret: `openssl rand -hex 32`
- **Permissions:**

| Permission | Access | Why |
|------------|--------|-----|
| Contents | Read | Fetch file contents and config files |
| Pull requests | Read & Write | Post review comments |
| Issues | Read & Write | Post summary comments |
| Commit statuses | Read & Write | Set the merge gate status |
| Variables | Read | Read per-repo Claude credentials |

- **Subscribe to events:** Pull requests, Issue comments

Click **Create GitHub App**, note the **App ID**, generate and download a **private key** (`.pem` file), then install the app on your org/repos.

### 2. Server

Run the webhook server with these environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | Yes | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY` | Yes | GitHub App private key (`.pem` contents) |
| `WEBHOOK_SECRET` | Yes | Webhook secret (must match GitHub App settings) |
| `SERVER_PRIVATE_KEY` | No* | RSA-4096 private key for decrypting per-repo Claude credentials |
| `CLAUDE_API_URL` | No* | Server-wide fallback Bedrock endpoint |
| `CLAUDE_API_TOKEN` | No* | Server-wide fallback Bedrock token |
| `GITHUB_APP_SLUG` | No | GitHub App slug (e.g. `hawkeye-reviewer`). Enables auto-requesting HawkEye as a reviewer on PR open and the "Re-request review" button |

\* At least one credential source must be configured: either `SERVER_PRIVATE_KEY` (enables per-repo encrypted credentials) or `CLAUDE_API_URL` + `CLAUDE_API_TOKEN` (server-wide fallback), or both.

> **Finding your app slug:** Go to your GitHub App's General settings page. The slug is the last segment of the public link URL shown there (e.g. if the URL is `https://github.com/apps/hawkeye-reviewer`, the slug is `hawkeye-reviewer`).

Generate `SERVER_PRIVATE_KEY` once and store it permanently:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096
```

Start the server:

```bash
python3 scripts/webhook_server.py
```

The server exposes:
- `POST /webhook` — receives GitHub webhook events
- `GET /health` — health check
- `GET /public-key` — serves the RSA public key for encrypting per-repo tokens

### 3. Per-repo Claude credentials

Each team encrypts their own Claude API token locally and stores the encrypted blob as GitHub repo variables. The server decrypts it at review time using `SERVER_PRIVATE_KEY`.

```bash
python3 scripts/encrypt_token.py --token "YOUR_BEDROCK_TOKEN"
```

Then set in the repo → **Settings → Secrets and variables → Actions → Variables**:
- `HAWKEYE_CLAUDE_API_URL` = your Bedrock endpoint URL
- `HAWKEYE_CLAUDE_BLOB` = encrypted blob from above

Example `HAWKEYE_CLAUDE_API_URL`:

```
https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-sonnet-4-20250514-v1:0/converse
```

### 4. Deployment (Azure App Service)

HawkEye is designed to run as a long-lived process on Azure App Service (Linux). The recommended setup:

1. **Startup command:** `python3 scripts/webhook_server.py`
2. **Environment variables:** Set all variables from the table above in **Configuration → Environment variables** in the App Service portal
3. **Port:** The server listens on `PORT` env var (defaults to `8080`). Azure App Service sets `PORT` automatically — no change needed
4. **Health check:** Configure a health check at `/health` in **Monitoring → Health check**

When deploying updates, the App Service will restart automatically on the next GitHub Actions deploy run.

### 5. GitHub App IP Allow List (organizations with IP restrictions)

If your GitHub organization enforces an IP allow list (e.g. enterprise or security-hardened orgs), you must add your server's outbound IPs to the GitHub App's allow list:

1. Go to your GitHub App → **Advanced** → **IP allow list**
2. Add each of your server's outbound IPs

For Azure App Service, outbound IPs are listed in **Properties → Outbound IP addresses** and **Additional Outbound IP Addresses** in the portal. Add all of them — Azure may use any of these IPs for outbound connections.

## Architecture

The reviewer consists of two scripts:

- **`webhook_server.py`** — HTTP server that receives GitHub webhooks, handles GitHub App JWT authentication, posts placeholder comments, and dispatches reviews to a thread pool
- **`hawkeye_pr_review.py`** — the review engine: fetches PR data, assembles the prompt, calls Claude, and posts the results back to GitHub

Both scripts communicate with two external systems:

- **GitHub API** — reads PR metadata, changed files, file contents, config files, and existing comments; writes summary comments, inline review comments, and commit statuses
- **Amazon Bedrock** — sends the assembled prompt to Claude via the Converse API and parses the JSON response

### Event handling

| Event | Behavior |
|-------|----------|
| PR opened / reopened / ready for review | Posts a placeholder comment, runs a full review, sets commit status to `success` |
| New commits pushed (`synchronize`) | No automatic review. Use `@hawkeye review` or "Re-request review" to trigger manually. |
| `@hawkeye review` comment | Runs a full review on the current PR head. Deduplicates against existing comments. |
| "Re-request review" button | Runs a full re-review (requires `GITHUB_APP_SLUG` to be set). |

### Review output

1. **Summary comment** — overview of the PR, list of changes, file table, and count of inline comments by severity
2. **Inline comments** — posted on the relevant lines in the diff, with optional `suggestion` blocks for one-click fixes
3. **Commit status** — `HawkEye Review` on the head commit (`success`, `pending`, or `error`)

Every comment includes the reviewer version (e.g. `v1.4.0`) and an AI-generated content disclaimer.

## Review flow

<p align="center">
  <img src="diagrams/02_review_pipeline.png" alt="Review Pipeline">
</p>

The review follows a 12-step pipeline:

1. **Webhook received** — GitHub sends event on PR open/reopen/ready/comment
2. **Validate signature** — HMAC-SHA256 verification against webhook secret
3. **Generate GitHub App token** — JWT signed with RSA private key, exchanged for an installation access token
4. **Post placeholder comment** — immediate feedback while Claude analyzes
5. **Set commit status to pending** — merge gate activated
6. **Fetch PR metadata** — title, description, head SHA
7. **Fetch changed files** — paginated diffs and file statuses
8. **Build context layers** — repo configs, docs, guidelines, tree, siblings, imports, linter configs
9. **Build prompt** — assemble all context + changed files (up to ~180K characters)
10. **Call Claude via Bedrock** — Converse API, 180s timeout, bearer token auth
11. **Parse JSON response** — extract summary + comments array
12. **Filter and deduplicate** — validate against diff lines, remove duplicates
13. **Post review to GitHub** — summary comment, inline comments, commit status

If the review returns 0 comments on a PR with 150+ additions, a second-pass review is triggered with a diff-only prompt to catch anything missed.

```
PR triggered
     │
     ▼
Fetch all changed files from GitHub API
(filename, status, patch, changes, additions, deletions)
     │
     ├─ removed files ──► dropped everywhere
     │
     ▼
Compute: reviewable_count, total_changes
     │
     ├─ reviewable_count == 0 ──► "deletions only" early exit
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  use_map_reduce = files >= 8  OR  changes >= 1500   │
└─────────────────────────────────────────────────────┘
          │                          │
         NO                         YES
          │                          │
          ▼                          ▼
   Single Claude call          Group files into batches
   budget: 180k chars          (by directory, max 8/batch)
          │                          │
          │                    ┌─────┴──────┐
          │                    │  Per batch  │  (up to 5 parallel)
          │                    │  budget:    │
          │                    │  120k chars │
          │                    └─────┬──────┘
          │                          │
          └──────────┬───────────────┘
                     │
                     ▼
           Per-file content decision (same logic both paths):
           ┌──────────────────────────────────────────────┐
           │  changes > 800?                              │
           │    YES → diff only (patch)                   │
           │    NO  → fetch full source from contents API │
           │          → smart file block:                 │
           │            · file ≤ threshold lines?         │
           │              full source + patch             │
           │            · file > threshold lines?         │
           │              imports + ±context lines + patch│
           │          → if block > budget → fallback to   │
           │            diff only                         │
           │          → still > budget → skip             │
           └──────────────────────────────────────────────┘
                     │
                     ▼
           Context fetched alongside files:
           · repo_context  (configs: package.json, pyproject, etc.)
           · repo_docs     (README, CONTRIBUTING, etc.)
           · guidelines    (HAWKEYE_REVIEW_GUIDELINES.md etc.)
           · linter_config (eslint, prettier, ruff, etc.)
           · repo_tree     (full file tree)
           · sibling_files (other files in same dirs, unmodified)
           · imported_files (local modules imported by changed files)
           · related_context (test files, build configs for changed files)
                     │
          ┌──────────┴───────────────┐
         NO                         YES (map-reduce)
          │                          │
          ▼                          ▼
   → 1 Claude call            MAP: N Claude calls (parallel)
     all files + context      each batch: its files + context
          │                   (sibling/import/related per batch)
          │                          │
          │                          ▼
          │                   REDUCE: 1 Claude call
          │                   input: all batch results +
          │                          all diffs (150k char budget)
          │                   output: deduplicated, validated,
          │                           + new cross-file comments
          │                          │
          │                    reduce failed? → concat all batch
          │                    comments as fallback
          │                          │
          └──────────┬───────────────┘
                     │
                     ▼
           Filter comments to valid diff lines only
                     │
          ┌──────────┴──────────┐
         NO (single)           YES (map-reduce)
          │                     └──► done
          ▼
   Zero comments AND additions >= threshold?
     YES → second-pass retry with stricter prompt
           (diff-only, no full source)
     NO  → done
                     │
                     ▼
           Post review to GitHub
```

## Large PR support (map-reduce)

For PRs with **8+ files** or **1500+ total changes**, the reviewer automatically activates a map-reduce pipeline instead of the single-pass review:

```
Small PR  →  single-pass review (unchanged)

Large PR  →  MAP:     parallel batch reviews (5-8 files each, full context)
          →  REDUCE:  cross-file consolidation (dedup, validate, enhance)
          →  filter + post (shared path)
```

### How it works

1. **Grouping** — files are grouped into batches by directory affinity (files in the same directory stay together), with up to 8 files per batch
2. **Map phase** — each batch is reviewed in parallel via `ThreadPoolExecutor`, with batch-specific sibling files and imports for local context, plus full PR scope awareness
3. **Reduce phase** — a consolidation pass deduplicates comments, validates suggestions, and detects cross-file issues (broken contracts, mismatched interfaces, missing test updates)
4. **Shared context** — repo configs, docs, guidelines, and the directory tree are computed once and reused across all batches via a thread-safe file content cache

### Failure handling

- If some batches fail, the review is posted as **partial** with a warning in the summary and `failure` commit status to block merges
- Deletions-only PRs (zero reviewable files) exit early with a "no reviewable files" status

## Context window

<p align="center">
  <img src="diagrams/03_context_window.png" alt="Context Window">
</p>

The reviewer assembles a rich context window for Claude, organized into budget-controlled layers:

| Layer | Budget | Description |
|-------|--------|-------------|
| PR information | ~2K | Title + description (first 2,000 characters) |
| Repository context | 12K | Language configs (`pyproject.toml`, `package.json`, `tsconfig.json`, `go.mod`, etc.) |
| Repository tree | 8K | Full directory listing via Git Trees API, with focused subtrees for large repos |
| Sibling files | 18K | Up to 5 files total, with at most 3 per directory (same extension, ranked by name similarity, min 0.3 relevance) |
| Imported modules | 20K | Local modules referenced by `import`/`require()` in changed files |
| Linter/formatter configs | 12K | Active linter rules from 64+ config file patterns |
| Project documentation | 8K | `README.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, `CLAUDE.md`, `.cursorrules` |
| Custom guidelines | 4K | `.github/hawkeye-review.md` — team-specific review instructions |
| Related context | 15K | Auto-inferred test files and build configs |
| Changed files (PR diff) | 180K | Full content + unified diff (smart context for large files) |

### Smart file inclusion

For files over 200 lines, full file content is replaced with an optimized representation:

- **Header + imports** (first 30 lines) for module-level context
- **Expanded diff hunks** (40 lines of context around each changed region)
- **Structural signatures** (`def`, `class`, `fn`, `struct`, `interface`, etc.) from omitted sections so Claude still sees the file's shape

If the expanded context covers >70% of the file, the full content is sent instead. Estimated savings: **40-60% of file content tokens**.

### Smart context extraction

- **`package.json`** — only essential keys are extracted (name, scripts, dependencies, engines, workspaces); noise like browserslist/jest/babel config is dropped
- **`README.md`** (context files) — truncated at the 3rd `##` heading to keep the project description while dropping installation/license/contributing boilerplate

### Repository structure

The reviewer fetches the full directory tree (via the Git Trees API — a single API call) so Claude can see the entire project layout. This enables feedback like:

- "Your other services in `src/services/` use snake_case — this new file should too"
- "This file belongs in `src/utils/`, not `src/helpers/` based on your existing structure"
- "You already have a `BaseRepository` class — this new repository should extend it"

Noisy directories (`node_modules`, `__pycache__`, `dist`, `build`, `.git`, etc.) are automatically excluded.

### Sibling files

For each directory containing changed files, the reviewer fetches up to 5 existing source files total, with at most 3 from any single directory. Files are matched by extension and ranked by name similarity (minimum 0.3 relevance score). Barrel files like `__init__.py` and `index.js` are filtered out. This lets Claude compare the PR's code against established patterns — class structure, error handling, naming conventions, and architecture.

### Import resolution

The reviewer parses `import` / `from ... import` / `require()` statements in the changed files and fetches the referenced local modules. This lets Claude verify that the changed code uses internal APIs correctly — right parameter types, proper return value handling, and interface compliance.

## Linter-aware suggestions

The reviewer automatically fetches linter and formatter configuration files from your repository so that every `suggestion` block in the review respects your project's rules. A suggestion that introduces a linter violation is treated as worse than no suggestion at all.

**Supported tools** (64+ config file patterns):

| Language | Tools |
|----------|-------|
| Python | ruff, flake8, pylint, mypy, isort, bandit, pyre |
| JavaScript / TypeScript | ESLint, Prettier, Biome, Deno |
| Go | golangci-lint |
| Rust | rustfmt, clippy |
| Ruby | RuboCop |
| Java / Kotlin | Checkstyle, Scalafmt |
| Swift | SwiftLint |
| PHP | PHP-CS-Fixer, PHPCS, PHPStan |
| C/C++ | clang-format, clang-tidy |
| General | Stylelint, markdownlint, EditorConfig |

When the Git Trees API is available, all 64+ patterns are checked via fast set lookup. If the tree is unavailable (truncated or failed), a high-signal subset of ~17 files is checked as fallback.

## Custom review guidelines

For anything auto-detection can't cover, create a **`.github/hawkeye-review.md`** (or `.hawkeye-review.md` at the repo root) with free-form instructions:

```markdown
- This project targets Python 3.11+
- We use SQLAlchemy 2.0 style (not legacy 1.x patterns)
- Prefer `pathlib` over `os.path`
- Ignore import ordering — handled by isort pre-commit hook
- This is a Spring Boot 3.x project on Java 21 — use Jakarta namespace, not javax
- Skip architecture/design suggestions, focus only on correctness
```

These instructions are injected directly into the review prompt and take precedence over default review behavior.

## What it reviews

The reviewer works through a comprehensive checklist for every changed file:

| Category | What it checks |
|----------|---------------|
| **Correctness** | Null dereferences, off-by-one errors, integer overflow, empty collection handling, boundary values |
| **Security** | Hardcoded secrets, SQL injection, XSS, path traversal, SSRF, insecure deserialization, overly broad permissions |
| **Concurrency** | Race conditions, missing locks, deadlock potential, TOCTOU, unsafe publication |
| **Resource management** | Unclosed connections/handles, missing context managers, memory leaks, unbounded caches |
| **Error handling** | Swallowed exceptions, generic catch-alls, missing cleanup in error paths, unhelpful error messages |
| **Test coverage** | Missing tests for new logic, weak assertions, missing edge case tests, flaky test patterns |
| **API contracts** | Breaking changes, missing input validation, inconsistent error formats |
| **Design** | Algorithm/data structure choices, language-specific optimizations, architectural decisions, library suggestions, scalability concerns |
| **Dead code** | Commented-out code, unreachable code, unused variables/imports/functions, dead feature-flag branches, leftover debug statements |

## Severity levels

| Severity | Icon | Use case |
|----------|------|----------|
| Critical | :rotating_light: | Bugs, security vulnerabilities, data loss risks |
| Warning | :warning: | Error handling gaps, race conditions, resource leaks |
| Suggestion | :bulb: | Code improvements, better patterns, simplifications |
| Design | :triangular_ruler: | Architecture, algorithms, language optimizations, infra choices |
| Nitpick | :mag: | Minor observations, optional improvements |

## Noise control

Several layers work together to keep the review signal-to-noise ratio high.

### Hard constraints in the prompt
- **Diff-only scope** — comments are only allowed on lines that appear as added (`+`) in the diff; pre-existing code cannot be flagged
- **No style/formatting comments** — minor preferences are explicitly excluded
- **Linter compliance** — all suggestions must comply with your project's linter/formatter configs; a suggestion that would cause a linter error is treated as worse than no suggestion at all
- **Concise messages** — each comment is capped at 1-3 sentences
- **Compatibility check** — patterns incompatible with the project's declared runtime, frameworks, or dependencies are not suggested

### Post-response filtering (always applied)
After Claude responds, every comment is validated in code:
- Comments on lines not present in the diff are dropped (catches hallucinated line numbers)
- Comments matching an already-posted `(path, line, severity)` tuple are deduplicated — re-reviews don't repeat the same feedback

### Reduce-phase deduplication (map-reduce only)
The consolidation pass removes comments that flag the same issue on the same line across batches, and drops false positives that look incorrect given the full cross-file context.

### Custom guidelines (your escape hatch)
Create `.github/hawkeye-review.md` to suppress entire categories or set project-specific rules:

```markdown
- Skip architecture/design suggestions, focus only on correctness
- Ignore import ordering — handled by isort pre-commit hook
- This is internal tooling — do not flag missing input validation
```

Guidelines take precedence over all default review behavior.

### Design tradeoff
The reviewer intentionally errs toward thoroughness over conservatism — it would rather surface a potential concern that turns out to be fine than miss a real bug or security issue. Noise is managed structurally (scope, style filter, linter compliance, dedup, custom guidelines) rather than by asking Claude to hold back. If you find the volume too high, the main lever is `.github/hawkeye-review.md`.

## Cost and latency

### How many API calls per review

| PR size | Mode | API calls |
|---------|------|-----------|
| < 8 files and < 1500 changes | Single-pass | 1 (+ 1 optional retry if 0 comments) |
| ≥ 8 files or ≥ 1500 changes | Map-reduce | N batches + 1 reduce |

Map batches run in parallel, so wall time is `max(batch_time) + reduce_time`, not the sum.

### Token budget per call

Each call has a hard character budget that caps the prompt size:

| Call type | Input budget | Output cap |
|-----------|-------------|------------|
| Single-pass review | 180k chars (~50k tokens) | 16,384 tokens |
| Map-reduce batch | 120k chars (~35k tokens) | 16,384 tokens |
| Reduce (consolidation) | 150k chars diffs + 80k batch results | 16,384 tokens |
| Second-pass retry | Diff-only, no full source | 16,384 tokens |

~4 characters per token is a rough approximation — actual token counts vary by language and content.

### Estimated costs

Costs scale with PR size and model choice. Latency is dominated by the Claude API response time — typically 1–3 minutes per review.

**Claude Sonnet 4.6**

| PR size | Mode | API calls | Est. cost | Est. latency |
|---------|------|-----------|-----------|--------------|
| 500 lines, 4 files | Single-pass | 1 | ~$0.15 | ~1 min |
| 1K lines, 6 files | Single-pass | 1 | ~$0.25 | ~2 min |
| 2K lines, 12 files | Map-reduce | 3+1 = 4 | ~$0.55 | ~2–3 min |
| 5K lines, 25 files | Map-reduce | 4+1 = 5 | ~$1.00 | ~3–5 min |

**Claude Opus 4.6**

| PR size | Mode | API calls | Est. cost | Est. latency |
|---------|------|-----------|-----------|--------------|
| 500 lines, 4 files | Single-pass | 1 | ~$0.25 | ~1 min |
| 1K lines, 6 files | Single-pass | 1 | ~$0.50 | ~2 min |
| 2K lines, 12 files | Map-reduce | 3+1 = 4 | ~$1.00 | ~2–3 min |
| 5K lines, 25 files | Map-reduce | 4+1 = 5 | ~$2.00 | ~3–5 min |

> All cost figures are rough estimates. Actual cost depends on how much of the token budget each call uses — prompts rarely hit the ceiling, so real costs are often lower.

### What drives cost up

- **Large files** — files over 200 lines use smart context (expanded hunks + signatures), but still consume more tokens than small files
- **Many sibling/import files** — each batch fetches context files from the same directories; a codebase with many large modules costs more per batch
- **High batch count** — more files = more batches = more parallel map calls, each billed independently
- **Second-pass retry** — triggered automatically if a single-pass review returns 0 comments on a PR with 150+ additions

### Reducing cost

- Use a smaller/faster model (e.g. Claude Haiku) via the `HAWKEYE_CLAUDE_API_URL` repo variable
- Add a `.github/hawkeye-review.md` to skip whole review categories (e.g. "skip design suggestions") — fewer relevant findings means fewer tokens spent reasoning about them
- The smart file inclusion and structural signatures features already save an estimated 40–60% of file content tokens compared to sending full source for every file

## Optional: require review before merge

To use HawkEye's review as a merge gate:

1. Go to **Settings → Branches → Branch protection rules**
2. Enable **Require status checks to pass before merging**
3. Search for and add `HawkEye Review`

HawkEye sets the `HawkEye Review` commit status to `success` after each completed review. Pushing new commits does **not** trigger a re-review automatically — the status from the last review remains in place. To get a fresh review after new commits, use `@hawkeye review` in a PR comment or click "Re-request review" next to HawkEye in the Reviewers sidebar.

## Supported models

The reviewer auto-detects the Claude model from the Bedrock endpoint URL and displays it in the review footer:

- Claude Opus 4.6, 4.5, 4
- Claude Sonnet 4.6, 4.5, 4
- Claude Haiku 4.6, 4.5, 4
- Claude 3.7 Sonnet, 3.5 Sonnet, 3.5 Haiku
- Claude 3 Opus, 3 Sonnet, 3 Haiku
