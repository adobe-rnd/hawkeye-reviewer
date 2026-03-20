# Changelog

## [1.4.2] - 2026-03-20

### Bug Fixes

- **Map-reduce crash on every PR** — `_review_map_reduce_inner` was returning 4 values on two code paths (`(summary, comments, total_batches, failed_batches)` and the deletions-only early return `({}, [], 0, 0)`) but the caller unpacks exactly 3. This caused a `too many values to unpack` crash on every PR that triggered the map-reduce path. Fixed by removing the redundant `total_batches` from both return statements
- **Inline comments posted to wrong lines after deletions** — `get_diff_lines()` was incrementing the new-file line counter for deleted lines (`-`). In a unified diff, deleted lines don't advance the new-file side, so any added lines following a deletion would be mapped to an incorrect line number. Fixed by only incrementing `current_line` for added (`+`) and context lines
- **Diff header detection too broad** — The `---` / `+++` checks used to skip unified diff file headers (`--- a/path`, `+++ b/path`) would also match real content lines starting with `----` or `++++` (e.g. a Markdown `---` front matter line being deleted). Fixed by matching on the trailing space (`"--- "` / `"+++ "`) which is always present in file headers
- **`failed_b` NameError on single-pass reviews** — `failed_b` was only assigned inside the `use_map_reduce` branch. Although short-circuit evaluation prevented a crash in practice, the variable was formally undefined for the single-pass path. Fixed by initializing `failed_b = 0` before the branch

### Documentation

- **README diagrams** — Added and refined system architecture, review pipeline, prompt assembly, and single-pass vs map-reduce flow diagrams

---

## [1.4.1] - 2026-03-19

### Internal / Cleanup

- **README restructured** — split Setup into "Enroll Your Repo" (developer-facing, 3 steps) and "Administrator Setup" (one-time deployment guide)
- **Bug fixes and optimizations** — `URLError` now caught in `_github_request`, `read_repo_variables` parallelized with `ThreadPoolExecutor`, stale `_review_map_reduce_inner` type annotation fixed

---

## [1.4.0] - 2026-03-19

### New Features

- **Copilot-like review behaviour** — HawkEye now automatically requests itself as a reviewer when a PR is opened, so it appears in the Reviewers sidebar with a "Re-request review" button from the start
- **Auto re-review on push** — Removed. New commits pushed to an open PR do not trigger a re-review automatically. Use `@hawkeye review` or the "Re-request review" button to trigger manually
- **Re-request review button** — Clicking the circular arrow "Re-request review" next to HawkEye in the PR sidebar triggers a new review (`pull_request.review_requested` event). Requires `GITHUB_APP_SLUG` env var to be set
- **`@hawkeye review` mention trigger** — Replaced the `/hawkeye-review` slash command with `@hawkeye review` as the on-demand review trigger in PR comments, consistent with how Copilot is invoked. Case-insensitive
- **`GITHUB_APP_SLUG` config** — New optional env var that enables the self-reviewer-request and re-request-review features. Set to `hawkeye-reviewer` (or your app's slug) in Azure App Settings
- **GitHub App IP allow list support** — Added Azure outbound IPs to the GitHub App's IP allow list, enabling HawkEye to work in organizations with strict IP restrictions (e.g. OneAdobe)

### Performance Improvements

- **Parallel context fetching** — `get_repo_context`, `get_repo_docs`, `get_review_guidelines`, and `get_linter_config` are now fetched in parallel using `ThreadPoolExecutor(max_workers=4)` in both single-review and map-reduce paths, replacing sequential GitHub API calls. Reduces context-fetch latency by ~75%
- **File content cache scope expanded** — The in-process file content cache was previously only enabled during map-reduce reviews. It is now active for the full review lifecycle, preventing duplicate GitHub API fetches across the sibling, import, and related-context phases in single-review mode
- **Sibling candidate pre-filtering** — Sibling file candidates below the relevance threshold are now filtered before sorting, avoiding unnecessary sort work on entries that would be discarded

### Bug Fixes

- **Diff line number tracking** — `get_diff_lines()` was incorrectly adding context lines (unchanged lines) to the valid comment target set, causing review comments to be posted on lines not part of the diff. Fixed by removing the `else` branch that incremented and added context lines
- **Temp file permission race condition** — `generate_github_app_jwt()`, `decrypt_repo_token()`, and `get_server_public_key_pem()` created temp PEM files with default permissions then called `chmod` separately, leaving a window where private keys were world-readable. Fixed by using `tempfile.mkstemp()` which creates files with `0600` permissions atomically
- **Pagination error handling** — `get_changed_files()` pagination loop now wraps `github_get()` in a try/except, surfacing API errors with page context instead of crashing silently
- **Invalid `PLACEHOLDER_COMMENT_ID`** — Added try/except around `int()` conversion to handle malformed env var values gracefully instead of raising `ValueError`
- **File content cache race condition** — Changed `_file_content_cache[key] = content` to `_file_content_cache.setdefault(key, content)` to prevent concurrent threads from overwriting each other's cache entries
- **Sibling relevance threshold ineffective** — `_sibling_relevance()` previously returned `best + 0.5`, meaning files with zero word overlap always scored 0.5 — above the `SIBLING_MIN_RELEVANCE = 0.3` threshold. Changed to return `best` so the threshold is actually enforced
- **Token expiry parse failure now logged** — Silent fallback to 1-hour expiry when GitHub returns an unparseable `expires_at` date now emits a warning to stderr
- **Executor and server shutdown** — `executor.shutdown()` and `server.server_close()` moved into a `finally` block so they run on any exit path, not just `KeyboardInterrupt`
- **Error handler cleanup** — Separated `edit_comment` and `set_commit_status` into independent try/except blocks so a failure updating the placeholder comment doesn't prevent the commit status from being set

### Breaking Changes

- **`/hawkeye-review` slash command removed** — The on-demand trigger is now `@hawkeye review` as a PR comment. Any documentation, runbooks, or muscle memory referencing `/hawkeye-review` should be updated
- **`/hawkeye-review` slash command removed** — replaced by `@hawkeye review` (see New Features above)

### Internal / Cleanup

- **`tempfile` import moved to module level** in `webhook_server.py` (was inlined in 3 separate functions)
- **`review_map_reduce` return value simplified** — Removed the redundant `total_batches` return value; the caller already had this as `num_batches`. Signature is now `(summary, comments, failed_batches)`
- **`post_pending_status()` removed** — Function was dead code after the `synchronize` behaviour change
- Renamed bot, scripts, env vars, and triggers from `claude-*` / `CLAUDE_*` to `hawkeye-*` / `HAWKEYE_*` throughout
- Submitted reviews now use the formal GitHub Reviews API (`POST /pulls/{pr}/reviews`) so HawkEye appears in the PR's Reviewers section with inline comments attached to the review

---

## [1.3.1] - 2026-03-16

Configurable GitHub App ID support and webhook server bug fixes. See PR #12 and PR #13.
