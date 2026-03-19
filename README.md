# HawkEye Reviewer

AI-powered pull request reviews using Claude (Anthropic) via Amazon Bedrock. Provides senior-engineer-level review comments with inline suggestions, design feedback, and automatic repo context awareness — all from a single Python script with zero dependencies.

<p align="center">
  <img src="diagrams/01_system_architecture.png" alt="System Architecture">
</p>

---

## Contents

- [Features](#-features)
- [How It Works](#-how-it-works)
- [Setup](#-setup)
- [What HawkEye Reviews](#-what-hawkeye-reviews)
- [Context & Intelligence](#-context--intelligence)
- [Review Pipeline](#-review-pipeline)
- [Noise Control](#-noise-control)
- [Cost & Performance](#-cost--performance)
- [Supported Models](#-supported-models)

---

## ✨ Features

| | |
|---|---|
| 🔁 **Automatic reviews** | Triggered on every PR open, reopen, or ready-for-review |
| 💬 **On-demand reviews** | Comment `@hawkeye review` or click "Re-request review" in the sidebar |
| 👤 **Appears as a reviewer** | Requests itself when a PR opens — shows up in the Reviewers sidebar like Copilot |
| 📦 **Map-reduce pipeline** | Handles large PRs (8+ files or 1500+ changes) with parallel batches and cross-file consolidation |
| 🧠 **Full repo awareness** | Directory tree, sibling files, and imported modules for pattern-aware feedback |
| 🔧 **Linter-aware** | Fetches your linter/formatter configs (64+ patterns, 10+ languages) — suggestions never violate your rules |
| 📋 **Custom guidelines** | Optional `.github/hawkeye-review.md` for repo-specific instructions |
| ✏️ **Inline suggestions** | Native GitHub suggestion blocks for one-click fixes |
| 🚦 **5 severity levels** | Critical, warning, suggestion, design, nitpick |
| 🔕 **Deduplication** | Re-reviews skip comments already posted — no repeated feedback |
| ⏱️ **Instant placeholder** | Posts a comment immediately while Claude analyzes |
| 🔒 **Merge gate** | Sets a `HawkEye Review` commit status — use it in branch protection rules |
| 📝 **Draft-aware** | Skips draft PRs to avoid wasted API calls |
| 📦 **Zero dependencies** | Standard library only — no `pip install` required |

---

## ⚙️ How It Works

HawkEye runs as a **standalone webhook server** (`webhook_server.py`) that receives GitHub events and triggers the review engine as a subprocess.

```
GitHub event  →  webhook_server.py  →  hawkeye_pr_review.py  →  GitHub API
                  (auth, queue)         (prompt, Claude, post)
```

**Two scripts, two external systems:**

| Component | Role |
|-----------|------|
| `webhook_server.py` | Validates webhook signatures, generates GitHub App tokens, posts placeholder comments, dispatches reviews to a thread pool |
| `hawkeye_pr_review.py` | Fetches PR data, assembles the prompt, calls Claude via Bedrock, posts results back to GitHub |
| GitHub API | Read: PR metadata, diffs, file contents, existing comments. Write: review comments, commit status |
| Amazon Bedrock | Claude inference via the Converse API |

### Event handling

| Event | Behavior |
|-------|----------|
| PR opened / reopened / ready for review | Runs a full review, sets commit status to `success` |
| New commits pushed (`synchronize`) | **No automatic review.** Status from last review stays in place. |
| `@hawkeye review` comment | Runs a full review on the current head, deduplicating against existing comments |
| "Re-request review" button | Triggers a fresh review (requires `GITHUB_APP_SLUG` env var) |

### Review output

Each review produces three things:

1. **Summary comment** — PR overview, file table, change breakdown, and inline comment counts by severity
2. **Inline comments** — posted on relevant diff lines, with optional `suggestion` blocks for one-click fixes
3. **Commit status** — `HawkEye Review` on the head commit: `success`, `pending`, or `error`

Every comment includes the reviewer version (e.g. `v1.4.0`) and an AI-generated content disclaimer.

---

## 🚀 Setup

### 1. Create a GitHub App

Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**:

- **Name:** Any unique name (e.g. `HawkEye Reviewer`)
- **Homepage URL:** Your repo URL
- **Webhook:** Active ✓, URL: `https://your-server/webhook`, Secret: `openssl rand -hex 32`

**Permissions:**

| Permission | Access | Why |
|------------|--------|-----|
| Contents | Read | Fetch file contents and config files |
| Pull requests | Read & Write | Post review comments |
| Issues | Read & Write | Post summary comments |
| Commit statuses | Read & Write | Set the merge gate status |
| Variables | Read | Read per-repo Claude credentials |

**Subscribed events:** Pull requests, Issue comments

After creating the app: note the **App ID**, generate and download a **private key** (`.pem`), then install the app on your org or repos.

---

### 2. Configure the server

Set the following environment variables before starting the server:

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | ✅ | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY` | ✅ | GitHub App private key (`.pem` file contents) |
| `WEBHOOK_SECRET` | ✅ | Webhook secret (must match GitHub App settings) |
| `SERVER_PRIVATE_KEY` | ☑️ * | RSA-4096 private key for decrypting per-repo Claude credentials |
| `CLAUDE_API_URL` | ☑️ * | Server-wide fallback Bedrock endpoint |
| `CLAUDE_API_TOKEN` | ☑️ * | Server-wide fallback Bedrock token |
| `GITHUB_APP_SLUG` | ➕ | App slug (e.g. `hawkeye-reviewer`). Enables the Reviewers sidebar integration and "Re-request review" button |

\* At least one credential source is required: `SERVER_PRIVATE_KEY` (per-repo encrypted credentials), `CLAUDE_API_URL` + `CLAUDE_API_TOKEN` (server-wide fallback), or both.

> **Finding your app slug:** Go to your GitHub App's General settings page. The slug is the last segment of the public link URL (e.g. `https://github.com/apps/hawkeye-reviewer` → slug is `hawkeye-reviewer`).

Generate `SERVER_PRIVATE_KEY` once and store it permanently:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096
```

Start the server:

```bash
python3 scripts/webhook_server.py
```

**Endpoints:**
- `POST /webhook` — receives GitHub webhook events
- `GET /health` — health check
- `GET /public-key` — serves the RSA public key for encrypting per-repo tokens

---

### 3. Per-repo Claude credentials

Each team encrypts their own Claude token locally and stores the encrypted blob as a GitHub repo variable. The server decrypts it at review time using `SERVER_PRIVATE_KEY`.

```bash
python3 scripts/encrypt_token.py --token "YOUR_BEDROCK_TOKEN"
```

Then set in **repo → Settings → Secrets and variables → Actions → Variables**:

| Variable | Value |
|----------|-------|
| `HAWKEYE_CLAUDE_API_URL` | Your Bedrock endpoint URL |
| `HAWKEYE_CLAUDE_BLOB` | Encrypted blob from the command above |

Example endpoint URL:

```
https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-sonnet-4-20250514-v1:0/converse
```

---

### 4. Deploy to Azure App Service

| Setting | Value |
|---------|-------|
| **Startup command** | `python3 scripts/webhook_server.py` |
| **Environment variables** | Set all variables above in **Configuration → Environment variables** |
| **Port** | Reads `PORT` env var (defaults to `8080`). Azure sets this automatically. |
| **Health check** | Configure at `/health` in **Monitoring → Health check** |

---

### 5. IP allow list (organizations with IP restrictions)

If your GitHub organization enforces an IP allow list, add your server's outbound IPs to the GitHub App:

1. Go to your GitHub App → **Advanced** → **IP allow list**
2. Add each outbound IP

For Azure App Service, outbound IPs are listed under **Properties → Outbound IP addresses** and **Additional Outbound IP Addresses**. Add all of them — Azure may use any of these for outbound connections.

---

## 🔍 What HawkEye Reviews

HawkEye works through a comprehensive checklist on every changed file:

| Category | What it checks |
|----------|----------------|
| 🐛 **Correctness** | Null dereferences, off-by-one errors, integer overflow, empty collection handling, boundary values |
| 🔐 **Security** | Hardcoded secrets, SQL injection, XSS, path traversal, SSRF, insecure deserialization, overly broad permissions |
| ⚡ **Concurrency** | Race conditions, missing locks, deadlock potential, TOCTOU, unsafe publication |
| 🗂️ **Resource management** | Unclosed connections/handles, missing context managers, memory leaks, unbounded caches |
| ⚠️ **Error handling** | Swallowed exceptions, generic catch-alls, missing cleanup in error paths, unhelpful messages |
| 🧪 **Test coverage** | Missing tests for new logic, weak assertions, missing edge cases, flaky patterns |
| 📡 **API contracts** | Breaking changes, missing input validation, inconsistent error formats |
| 🏗️ **Design** | Algorithm/data structure choices, language-specific optimizations, architectural decisions, scalability concerns |
| 🗑️ **Dead code** | Commented-out code, unreachable paths, unused variables/imports/functions, leftover debug statements |

### Severity levels

| Severity | Icon | When it's used |
|----------|------|----------------|
| **Critical** | 🚨 | Bugs, security vulnerabilities, data loss risks |
| **Warning** | ⚠️ | Error handling gaps, race conditions, resource leaks |
| **Suggestion** | 💡 | Code improvements, better patterns, simplifications |
| **Design** | 📐 | Architecture, algorithms, language optimizations, infra choices |
| **Nitpick** | 🔍 | Minor observations, optional improvements |

### Merge gate

To require a passing HawkEye review before merging:

1. Go to **Settings → Branches → Branch protection rules**
2. Enable **Require status checks to pass before merging**
3. Search for and add `HawkEye Review`

Pushing new commits does **not** reset the status — the result from the last review stays. Use `@hawkeye review` or the "Re-request review" button to trigger a fresh review after new commits.

---

## 🧠 Context & Intelligence

<p align="center">
  <img src="diagrams/03_context_window.png" alt="Context Window">
</p>

HawkEye assembles a layered context window so Claude understands your codebase — not just the diff.

| Layer | Budget | Contents |
|-------|--------|----------|
| PR information | ~2K | Title + description |
| Repository context | 12K | `pyproject.toml`, `package.json`, `tsconfig.json`, `go.mod`, etc. |
| Repository tree | 8K | Full directory listing (Git Trees API, noisy dirs excluded) |
| Sibling files | 18K | Up to 5 existing files from changed directories (ranked by relevance) |
| Imported modules | 20K | Local modules referenced by `import`/`require()` in changed files |
| Linter/formatter configs | 12K | Active rules from 64+ config file patterns |
| Project documentation | 8K | `README.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, `.cursorrules` |
| Custom guidelines | 4K | `.github/hawkeye-review.md` — team-specific instructions |
| Related context | 15K | Auto-inferred test files and build configs |
| **Changed files (diff)** | **180K** | Full content + unified diff |

### Smart file inclusion

For files over 200 lines, full source is replaced with a compact representation that preserves signal while cutting tokens:

- **Header + imports** (first 30 lines) for module-level context
- **Expanded diff hunks** (40 lines of context around each changed region)
- **Structural signatures** — `def`, `class`, `fn`, `struct`, `interface`, etc. from omitted sections, so Claude still sees the file's shape

If the expanded context covers >70% of the file anyway, full content is sent instead. Estimated savings: **40–60% of file content tokens**.

### Repository structure

The full directory tree (a single Git Trees API call) lets Claude reason across the whole project layout:

> *"Your other services in `src/services/` use snake_case — this new file should too"*
> *"You already have a `BaseRepository` class — this new repository should extend it"*

Noisy directories (`node_modules`, `__pycache__`, `dist`, `build`, `.git`, etc.) are excluded automatically.

### Sibling files

For each directory containing changed files, HawkEye fetches up to 5 existing source files (max 3 per directory), matched by extension and ranked by name similarity. Barrel files (`__init__.py`, `index.js`) are filtered out. This lets Claude compare the PR's code against established patterns — class structure, error handling, naming, and architecture.

### Import resolution

HawkEye parses `import` / `from ... import` / `require()` in changed files and fetches the referenced local modules. This lets Claude verify that internal APIs are used correctly — right parameter types, proper return value handling, interface compliance.

### Linter-aware suggestions

HawkEye fetches your linter/formatter configs so every suggestion block respects your project's rules. A suggestion that would cause a linter violation is treated as worse than no suggestion at all.

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

### Custom review guidelines

Create **`.github/hawkeye-review.md`** (or `.hawkeye-review.md` at the repo root) with free-form instructions:

```markdown
- This project targets Python 3.11+
- We use SQLAlchemy 2.0 style (not legacy 1.x patterns)
- Prefer `pathlib` over `os.path`
- Ignore import ordering — handled by isort pre-commit hook
- Skip architecture/design suggestions, focus only on correctness
```

These instructions are injected directly into the review prompt and take precedence over all default behavior.

---

## 📊 Review Pipeline

<p align="center">
  <img src="diagrams/02_review_pipeline.png" alt="Review Pipeline">
</p>

Every review follows the same pipeline:

1. **Webhook received** — GitHub sends event on PR open/reopen/ready/comment
2. **Validate signature** — HMAC-SHA256 against webhook secret
3. **Generate installation token** — JWT signed with RSA private key, exchanged for a short-lived access token
4. **Post placeholder comment** — immediate feedback while Claude analyzes
5. **Fetch PR metadata** — title, description, head SHA
6. **Fetch changed files** — paginated diffs and file statuses
7. **Build context layers** — repo configs, docs, guidelines, tree, siblings, imports, linter configs (fetched in parallel)
8. **Build prompt** — assemble all context + changed files
9. **Call Claude via Bedrock** — Converse API, 180s timeout
10. **Parse JSON response** — extract summary + comments array
11. **Filter & deduplicate** — validate against diff lines, drop duplicates
12. **Post review** — summary comment, inline comments, commit status

### Single-pass vs map-reduce

```
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
          │                    │  120k chars │
          │                    └─────┬──────┘
          │                          │
          │                   REDUCE: 1 Claude call
          │                   (dedup, validate, cross-file)
          │                          │
          └──────────┬───────────────┘
                     │
                     ▼
           Filter to valid diff lines
                     │
          ┌──────────┴──────────────────┐
    map-reduce                       single-pass
       done                              │
                              0 comments AND >= 150 additions?
                                YES → second-pass (diff-only)
                                NO  → done
                                         │
                                         ▼
                                Post review to GitHub
```

**Map phase** — files are grouped by directory affinity (same-dir files stay together) and reviewed in parallel, each batch with its own sibling files, imports, and local context.

**Reduce phase** — a consolidation pass deduplicates comments, validates suggestions, and surfaces cross-file issues (broken contracts, mismatched interfaces, missing test updates). Shared context (repo configs, docs, tree) is computed once and reused across all batches.

**Failure handling** — if some batches fail, the review is posted as partial with a warning in the summary and a `failure` status to block merges. Deletions-only PRs exit early with a "no reviewable files" status.

---

## 🔇 Noise Control

HawkEye uses layered controls to keep the signal-to-noise ratio high.

**Prompt constraints** (enforced in the system prompt):
- **Diff-only scope** — comments only on lines added (`+`) in the diff; pre-existing code cannot be flagged
- **No style/formatting** — minor preferences are explicitly excluded
- **Linter compliance** — all suggestions must pass your configured linter rules
- **Concise messages** — each comment is capped at 1–3 sentences
- **Compatibility** — patterns incompatible with the project's declared runtime/frameworks are not suggested

**Post-response filtering** (always applied in code):
- Comments on lines not present in the diff are dropped (catches hallucinated line numbers)
- Comments matching an existing `(path, line, severity)` tuple are deduplicated

**Reduce-phase deduplication** (map-reduce only):
- Cross-batch duplicates removed
- False positives that look incorrect given full cross-file context are dropped

**Custom guidelines** (your escape hatch):
- Use `.github/hawkeye-review.md` to suppress entire categories or add project-specific rules — these take precedence over everything else

> HawkEye intentionally errs toward thoroughness over conservatism — it would rather surface a concern that turns out to be fine than miss a real bug. Noise is managed structurally, not by asking Claude to hold back. If the volume is too high, the main lever is `.github/hawkeye-review.md`.

---

## 💰 Cost & Performance

### API calls per review

| PR size | Mode | API calls |
|---------|------|-----------|
| < 8 files and < 1,500 changes | Single-pass | 1 (+ 1 optional retry if 0 comments) |
| ≥ 8 files or ≥ 1,500 changes | Map-reduce | N batches + 1 reduce |

Map batches run in parallel — wall time is `max(batch_time) + reduce_time`, not the sum.

### Token budgets

| Call type | Input budget | Output cap |
|-----------|-------------|------------|
| Single-pass | 180k chars (~50k tokens) | 16,384 tokens |
| Map-reduce batch | 120k chars (~35k tokens) | 16,384 tokens |
| Reduce (consolidation) | 150k chars diffs + 80k results | 16,384 tokens |
| Second-pass retry | Diff-only | 16,384 tokens |

### Estimated costs

Latency is dominated by Claude response time — typically **1–3 minutes** per review.

**Claude Sonnet 4.6**

| PR size | Mode | API calls | Est. cost | Est. latency |
|---------|------|-----------|-----------|--------------|
| 500 lines, 4 files | Single-pass | 1 | ~$0.15 | ~1 min |
| 1K lines, 6 files | Single-pass | 1 | ~$0.25 | ~2 min |
| 2K lines, 12 files | Map-reduce | 3+1 | ~$0.55 | ~2–3 min |
| 5K lines, 25 files | Map-reduce | 4+1 | ~$1.00 | ~3–5 min |

**Claude Opus 4.6**

| PR size | Mode | API calls | Est. cost | Est. latency |
|---------|------|-----------|-----------|--------------|
| 500 lines, 4 files | Single-pass | 1 | ~$0.25 | ~1 min |
| 1K lines, 6 files | Single-pass | 1 | ~$0.50 | ~2 min |
| 2K lines, 12 files | Map-reduce | 3+1 | ~$1.00 | ~2–3 min |
| 5K lines, 25 files | Map-reduce | 4+1 | ~$2.00 | ~3–5 min |

> Cost figures are estimates. Prompts rarely hit the character ceiling, so real costs are often lower.

### Reducing cost

- **Use a smaller model** — point `HAWKEYE_CLAUDE_API_URL` to Claude Haiku for a cheaper, faster review
- **Scope with guidelines** — add `.github/hawkeye-review.md` to skip whole categories (e.g. "skip design suggestions")
- **Smart file inclusion** already saves an estimated 40–60% of file content tokens by default

---

## 🤖 Supported Models

HawkEye auto-detects the Claude model from the Bedrock endpoint URL and displays it in the review footer:

- Claude Opus 4.6, 4.5, 4
- Claude Sonnet 4.6, 4.5, 4
- Claude Haiku 4.6, 4.5, 4
- Claude 3.7 Sonnet, 3.5 Sonnet, 3.5 Haiku
- Claude 3 Opus, 3 Sonnet, 3 Haiku
