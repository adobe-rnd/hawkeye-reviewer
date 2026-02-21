# Claude PR Reviewer

AI-powered pull request reviews using Claude (Anthropic) via Amazon Bedrock. Provides Copilot-style review comments with an overview, file-by-file summaries, and inline suggestions with code fixes.

## Features

- **Automatic reviews** on every PR (opened, reopened, ready for review)
- **On-demand reviews** via `/claude-review` comment for re-reviewing after new commits
- **Inline code suggestions** with GitHub's native suggestion blocks (one-click apply)
- **Severity levels** — critical, warning, suggestion, nitpick — each with distinct icons
- **Instant feedback** — posts a placeholder comment immediately while Claude analyzes
- **Merge gate** — sets a commit status (`Claude Bedrock PR Review`) that can be required in branch protection rules
- **Smart invalidation** — new commits automatically set the status to "pending" so stale reviews don't block merges
- **Draft PR aware** — skips draft PRs to avoid wasting API calls
- **Zero dependencies** — the review script uses only Python's standard library (no `pip install`)

## Setup

### 1. Repository secrets

Define these secrets in your repository (Settings > Secrets and variables > Actions):

| Secret | Required | Description |
|--------|----------|-------------|
| `CLAUDE_REVIEWER_APP_PRIVATE_KEY` | Yes | The GitHub App's private key (`.pem` contents) |
| `CLAUDE_API_URL` | Yes | Bedrock converse endpoint URL (e.g. `https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-opus-4-5-20251101-v1:0/converse`) |
| `CLAUDE_API_TOKEN` | Yes | Bearer token for the Bedrock API |

### 2. GitHub App installation

The action uses a GitHub App for its bot identity (custom name and avatar). The App must be installed on the repository where you want reviews.

**Required App permissions:**
- **Contents**: Read
- **Pull requests**: Read and write
- **Issues**: Read and write (for posting comments)
- **Commit statuses**: Read and write (for the merge gate)

### 3. Workflow file

Create `.github/workflows/ai-pr-review.yml` in your repository:

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
        uses: adobe-rnd/claude-pr-reviewer@v1.0.0
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
| `/claude-review` comment | Runs a full review on the current PR head. Only works for users with repo association (members, collaborators, contributors). |

### Review output

1. **Summary comment** — overview of the PR, list of changes, file table, and count of inline comments by severity
2. **Inline comments** — posted directly on the relevant lines in the diff, with optional `suggestion` blocks for one-click fixes
3. **Commit status** — `Claude Bedrock PR Review` status on the head commit (`success`, `pending`, or `error`)

## Optional: require review before merge

To use Claude's review as a merge gate:

1. Go to **Settings > Branches > Branch protection rules**
2. Enable **Require status checks to pass before merging**
3. Search for and add `Claude Bedrock PR Review`

When new commits are pushed, the status is automatically set to `pending`, requiring either a new `/claude-review` or a new PR event to pass.

## Supported models

The action automatically detects the Claude model from the Bedrock endpoint URL and displays it in the review footer. Supported models include:

- Claude Opus 4.6, 4.5, 4
- Claude Sonnet 4.6, 4.5, 4
- Claude Haiku 4.6, 4.5, 4
- Claude 3.7 Sonnet, 3.5 Sonnet, 3.5 Haiku
- Claude 3 Opus, 3 Sonnet, 3 Haiku

## Action inputs

| Input | Required | Description |
|-------|----------|-------------|
| `app-private-key` | Yes | GitHub App private key (`.pem` contents) |
| `claude-api-url` | No | Bedrock converse endpoint URL. Not required for `synchronize` events (invalidate-only). |
| `claude-api-token` | No | Bedrock API bearer token. Not required for `synchronize` events (invalidate-only). |

`claude-api-url` and `claude-api-token` are marked as not required because `synchronize` events only set a pending status via curl and never call Claude. For all other events, both are required and the script will exit with an error if they are missing.
