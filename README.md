# Claude PR Bot Reviewer

Made to copy GitHub Copilot PR Reviewer functionalities.

Uses Claude hosted on AWS Bedrock.

Just create in your repo this workflow file `.github/workflows/ai-pr-review.yml`:

```yaml
name: AI PR Review (Claude via Bedrock)

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  ai_pr_review:
    runs-on: ubuntu-latest
    steps:
      - name: Claude Bedrock PR Review
        uses: adobe-rnd/claude-pr-reviewer@v1
        with:
          app-private-key: ${{ secrets.CLAUDE_REVIEWER_APP_PRIVATE_KEY }}
          claude-api-url: ${{ secrets.CLAUDE_API_URL }}
          claude-api-token: ${{ secrets.CLAUDE_API_TOKEN }}
```

Keep in mind you will have to define the variables `CLAUDE_API_URL` and `CLAUDE_API_TOKEN` in your repository based
on your Bedrock config.
