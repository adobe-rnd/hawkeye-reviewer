# Claude Bedrock PR Reviewer

AI-powered pull request reviews using Claude (Anthropic) via Amazon Bedrock. Provides senior-engineer-level review comments with inline suggestions, design feedback, and automatic repo context awareness.

## Features

- **Automatic reviews** on every PR (opened, reopened, ready for review)
- **On-demand reviews** via `/claude-review` comment for re-reviewing after new commits
- **Large PR support** — handles 1000+ line PRs with diff-only fallback for oversized files
- **Full repository awareness** — fetches the directory tree, sibling files, and imported modules so Claude understands your project structure, coding patterns, and internal APIs — not just the diff
- **Custom guidelines** — optional `.github/claude-review.md` for repo-specific review instructions
- **Inline code suggestions** with GitHub's native suggestion blocks (one-click apply)
- **5 severity levels** — critical, warning, suggestion, design, nitpick — each with distinct icons
- **Senior-level review checklist** — security, concurrency, edge cases, resource management, test coverage, API contracts, design improvements, and dead code detection
- **Comment deduplication** — skips duplicate comments on re-review so you don't get the same feedback twice
- **Instant feedback** — posts a placeholder comment immediately while Claude analyzes
- **Merge gate** — sets a commit status (`Claude Bedrock PR Review`) that can be required in branch protection rules
- **Smart invalidation** — new commits automatically set the status to "pending" so stale reviews don't block merges
- **Draft PR aware** — skips draft PRs to avoid wasting API calls
- **Zero dependencies** — uses only Python's standard library (no `pip install`)

## Setup

### 1. Secrets and variables

`CLAUDE_REVIEWER_APP_PRIVATE_KEY` should be set as an **organization-level** secret so it's available to all repos automatically (**Settings > Secrets and variables > Actions** at the org level).

The remaining secrets can be set at the org or repo level:

| Secret | Level | Required | Description |
|--------|-------|----------|-------------|
| `CLAUDE_REVIEWER_APP_PRIVATE_KEY` | Org | Yes | The GitHub App's private key (`.pem` contents) |
| `CLAUDE_API_URL` | Org or Repo | Yes | Bedrock converse endpoint URL (see example below) |
| `CLAUDE_API_TOKEN` | Org or Repo | Yes | Bearer token for the Bedrock API |

Example `CLAUDE_API_URL`:

```
https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-sonnet-4-20250514-v1:0/converse
```

### 2. GitHub App

The action uses a GitHub App for its bot identity. Install the App on your repository with these permissions:

| Permission | Access | Why |
|------------|--------|-----|
| Contents | Read | Fetch file contents and config files |
| Pull requests | Read & Write | Post review comments |
| Issues | Read & Write | Post summary comments |
| Commit statuses | Read & Write | Set the merge gate status |

### 3. Workflow file

Create `.github/workflows/ai-pr-review.yml`:

```yaml
name: AI PR Review (Claude via Bedrock)

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write

jobs:
  ai_pr_review:
    runs-on: ubuntu-latest
    if: >-
      (github.event_name == 'pull_request' && github.event.pull_request.draft == false) ||
      (github.event_name == 'issue_comment' &&
       github.event.issue.pull_request &&
       contains(github.event.comment.body, '/claude-review') &&
       github.event.comment.author_association != 'NONE')
    steps:
      - name: Claude Bedrock PR Review
        uses: adobe-rnd/claude-pr-reviewer@v1
        with:
          app-private-key: ${{ secrets.CLAUDE_REVIEWER_APP_PRIVATE_KEY }}
          claude-api-url: ${{ secrets.CLAUDE_API_URL }}
          claude-api-token: ${{ secrets.CLAUDE_API_TOKEN }}
```

That's it. The action handles everything else.

## How it works

| Event | Behavior |
|-------|----------|
| PR opened / reopened / ready for review | Posts a placeholder comment, runs a full review, sets commit status to `success` |
| New commits pushed (`synchronize`) | Sets commit status to `pending` (invalidates previous review). No new review runs automatically. |
| `/claude-review` comment | Runs a full review on the current PR head. Deduplicates against existing comments. Only works for users with repo association. |

### Review output

1. **Summary comment** — overview of the PR, list of changes, file table, and count of inline comments by severity
2. **Inline comments** — posted on the relevant lines in the diff, with optional `suggestion` blocks for one-click fixes
3. **Commit status** — `Claude Bedrock PR Review` on the head commit (`success`, `pending`, or `error`)

## Repository context

The reviewer automatically fetches config files from your repo to understand the tech stack. This prevents false positives like suggesting Python 3.9 syntax when your project targets 3.11.

**Auto-detected config files** (fetched if they exist, silently skipped if not):

| Language | Files |
|----------|-------|
| Python | `pyproject.toml`, `setup.cfg`, `setup.py`, `.python-version`, `requirements.txt`, `Pipfile` |
| JavaScript / TypeScript | `package.json`, `tsconfig.json`, `.nvmrc`, `.node-version` |
| Java / Kotlin | `pom.xml`, `build.gradle`, `build.gradle.kts` |
| Scala | `build.sbt`, `project/build.properties` |
| Go | `go.mod` |
| Rust | `Cargo.toml` |
| Containers | `Dockerfile`, `docker-compose.yml`, `docker-compose.yaml` |
| General | `.tool-versions`, `.editorconfig` |

**Auto-detected documentation files:**

| File | Purpose |
|------|---------|
| `README.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md` | Project documentation |
| `CLAUDE.md`, `.cursorrules`, `.cursor/rules/review.md`, `.cursor/rules/code-style.md` | AI coding conventions |
| `.github/CODEOWNERS`, `.github/pull_request_template.md` | GitHub conventions |

### Repository structure

The reviewer fetches the full directory tree (via the Git Trees API — a single API call) so Claude can see the entire project layout. This enables feedback like:

- "Your other services in `src/services/` use snake_case — this new file should too"
- "This file belongs in `src/utils/`, not `src/helpers/` based on your existing structure"
- "You already have a `BaseRepository` class — this new repository should extend it"

Noisy directories (`node_modules`, `__pycache__`, `dist`, `build`, `.git`, etc.) are automatically excluded.

### Sibling files

For each directory containing changed files, the reviewer fetches up to 3 existing source files with the same extension. This lets Claude compare the PR's code against established patterns in the same directory — class structure, error handling, naming conventions, and architectural patterns.

### Import resolution

The reviewer parses `import` / `from ... import` / `require()` statements in the changed files and fetches the referenced local modules. This lets Claude verify that the changed code uses internal APIs correctly — right parameter types, proper return value handling, and interface compliance.

## Custom review guidelines

For anything auto-detection can't cover, create a **`.github/claude-review.md`** (or `.claude-review.md` at the repo root) with free-form instructions:

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

## Optional: require review before merge

To use Claude's review as a merge gate:

1. Go to **Settings > Branches > Branch protection rules**
2. Enable **Require status checks to pass before merging**
3. Search for and add `Claude Bedrock PR Review`

When new commits are pushed, the status is automatically set to `pending`, requiring either a new `/claude-review` or a new PR event to pass.

## Supported models

The action auto-detects the Claude model from the Bedrock endpoint URL and displays it in the review footer:

- Claude Opus 4.6, 4.5, 4
- Claude Sonnet 4.6, 4.5, 4
- Claude Haiku 4.6, 4.5, 4
- Claude 3.7 Sonnet, 3.5 Sonnet, 3.5 Haiku
- Claude 3 Opus, 3 Sonnet, 3 Haiku

## Action inputs

| Input | Required | Description |
|-------|----------|-------------|
| `app-private-key` | Yes | GitHub App private key (`.pem` contents) |
| `claude-api-url` | No* | Bedrock converse endpoint URL |
| `claude-api-token` | No* | Bedrock API bearer token |

*`claude-api-url` and `claude-api-token` are not required for `synchronize` events (which only set a pending status). For all other events, both are required.
