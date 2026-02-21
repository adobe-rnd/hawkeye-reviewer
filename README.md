# Claude PR Bot Reviewer

Made to copy GitHub Copilot PR Reviewer functionalities.

Uses Claude hosted on AWS Bedrock.

Just create in your repo this workflow file `.github/workflows/ai-pr-review.yml`:

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
       contains(github.event.comment.body, '/claude-review'))
    steps:
      - name: Claude Bedrock PR Review
        uses: adobe-rnd/claude-pr-reviewer@v1.0.0
        with:
          app-private-key: ${{ secrets.CLAUDE_REVIEWER_APP_PRIVATE_KEY }}
          claude-api-url: ${{ secrets.CLAUDE_API_URL }}
          claude-api-token: ${{ secrets.CLAUDE_API_TOKEN }}
```

Keep in mind you will have to define the variables `CLAUDE_API_URL` and `CLAUDE_API_TOKEN` in your repository based
on your Bedrock config.
