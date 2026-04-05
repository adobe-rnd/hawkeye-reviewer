#!/usr/bin/env python3
"""CircleCI runner for HawkEye Reviewer on GHES.

Runs inside a CircleCI GHES job.  Reads CircleCI environment variables to
discover the repo and branch, authenticates via the GHES GitHub App, finds
the open PR for the branch, and runs hawkeye_pr_review.py directly.

Zero pip dependencies — uses only the Python standard library + openssl.

Required environment (typically from a shared CircleCI context):
  GITHUB_API_URL          https://git.corp.adobe.com/api/v3
  GHES_APP_ID             GitHub App ID on GHES
  GHES_APP_PRIVATE_KEY    GitHub App private key (PEM contents)

Optional environment:
  GHES_APP_SLUG           App slug (enables "Re-request review" button)
  GHES_SSL_CA_BUNDLE      CA bundle for GHES TLS (inline PEM or file path)
  SERVER_PRIVATE_KEY      RSA key for decrypting HAWKEYE_CLAUDE_BLOB

Claude credentials (per-repo, checked in order):
  1. CLAUDE_API_URL + CLAUDE_API_TOKEN   (CircleCI project env vars, plaintext)
  2. HAWKEYE_CLAUDE_API_URL + HAWKEYE_CLAUDE_BLOB  (CircleCI project env vars, encrypted)
  3. .hawkeye/credentials file in the repo          (encrypted)

CircleCI provides automatically:
  CIRCLE_PROJECT_USERNAME   repo owner
  CIRCLE_PROJECT_REPONAME   repo name
  CIRCLE_BRANCH             branch name
  CIRCLE_SHA1               commit SHA
"""

import json
import os
import subprocess
import sys
import time

# Allow imports from the same directory (scripts/)
sys.path.insert(0, os.path.dirname(__file__))

from webhook_server import (  # noqa: E402
    GitHubAPIError,
    _ca_bundle_path,
    _github_request,
    decrypt_repo_token,
    generate_github_app_jwt,
    get_installation_token,
    post_placeholder_comment,
    read_repo_credentials_file,
    request_self_as_reviewer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_pr_for_branch(
    api_url: str,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    ca_bundle: str | None,
) -> int | None:
    """Find the open PR number whose head branch matches *branch*."""
    url = (
        f"{api_url}/repos/{owner}/{repo}/pulls"
        f"?head={owner}:{branch}&state=open&per_page=1"
    )
    try:
        prs = _github_request("GET", url, token, ca_bundle=ca_bundle)
        if prs:
            return prs[0]["number"]
    except GitHubAPIError as exc:
        print(f"WARNING: Failed to query PRs: {exc}", file=sys.stderr)
    return None


def find_installation_id(
    api_url: str,
    jwt: str,
    owner: str,
    ca_bundle: str | None,
) -> int | None:
    """Find the App installation ID for *owner* (org or user)."""
    try:
        installations = _github_request(
            "GET", f"{api_url}/app/installations", jwt, ca_bundle=ca_bundle,
        )
    except GitHubAPIError as exc:
        print(f"ERROR: Failed to list installations: {exc}", file=sys.stderr)
        return None
    for inst in installations:
        if inst.get("account", {}).get("login", "").lower() == owner.lower():
            return inst["id"]
    return None


def resolve_claude_credentials(
    api_url: str,
    token: str,
    owner: str,
    repo: str,
    ca_bundle: str | None,
) -> tuple[str, str]:
    """Resolve Claude API URL and token for this repo.

    Lookup order (first match wins):
      1. CLAUDE_API_URL + CLAUDE_API_TOKEN  (plaintext CircleCI env vars)
      2. HAWKEYE_CLAUDE_API_URL + HAWKEYE_CLAUDE_BLOB  (encrypted CircleCI env vars)
      3. .hawkeye/credentials file in the repo  (encrypted)
    """
    # 1. Plaintext env vars
    url = os.environ.get("CLAUDE_API_URL", "").strip()
    tok = os.environ.get("CLAUDE_API_TOKEN", "").strip()
    if url and tok:
        return url, tok

    has_server_key = bool(os.environ.get("SERVER_PRIVATE_KEY"))

    # 2. Encrypted env vars
    if has_server_key:
        url = os.environ.get("HAWKEYE_CLAUDE_API_URL", "").strip()
        blob = os.environ.get("HAWKEYE_CLAUDE_BLOB", "").strip()
        if url and blob:
            try:
                return url, decrypt_repo_token(blob)
            except Exception as exc:
                print(
                    f"WARNING: Failed to decrypt HAWKEYE_CLAUDE_BLOB env var: {exc}",
                    file=sys.stderr,
                )

    # 3. .hawkeye/credentials file
    if has_server_key:
        try:
            creds = read_repo_credentials_file(
                api_url, token, owner, repo, ca_bundle,
            )
            file_url = creds.get("HAWKEYE_CLAUDE_API_URL", "").strip()
            file_blob = creds.get("HAWKEYE_CLAUDE_BLOB", "").strip()
            if file_url and file_blob:
                return file_url, decrypt_repo_token(file_blob)
        except Exception as exc:
            print(
                f"WARNING: Failed to read/decrypt .hawkeye/credentials: {exc}",
                file=sys.stderr,
            )

    return "", ""


def update_placeholder_error(
    api_url: str,
    token: str,
    owner: str,
    repo: str,
    placeholder_id: int,
    ca_bundle: str | None,
    message: str,
) -> None:
    """Update the placeholder comment with an error message."""
    if not placeholder_id:
        return
    try:
        body = (
            "<h2>\u274c HawkEye Reviewer \u2014 review failed</h2>\n\n"
            f"{message}\n\n"
            "Comment `@hawkeye review` to retry."
        )
        _github_request(
            "PATCH",
            f"{api_url}/repos/{owner}/{repo}/issues/comments/{placeholder_id}",
            token,
            {"body": body},
            ca_bundle=ca_bundle,
        )
    except Exception as exc:
        print(f"Failed to update placeholder: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # -- CircleCI environment --------------------------------------------------
    owner = os.environ.get("CIRCLE_PROJECT_USERNAME", "")
    repo = os.environ.get("CIRCLE_PROJECT_REPONAME", "")
    branch = os.environ.get("CIRCLE_BRANCH", "")
    sha = os.environ.get("CIRCLE_SHA1", "")

    if not owner or not repo or not branch:
        print("ERROR: Missing CircleCI env vars "
              "(CIRCLE_PROJECT_USERNAME, CIRCLE_PROJECT_REPONAME, CIRCLE_BRANCH)",
              file=sys.stderr)
        sys.exit(1)

    print(f"[HawkEye] Repo: {owner}/{repo}  Branch: {branch}  SHA: {sha[:8]}")

    # -- GHES config -----------------------------------------------------------
    api_url = os.environ.get("GITHUB_API_URL", "https://git.corp.adobe.com/api/v3")
    app_id = os.environ.get("GHES_APP_ID", "")
    private_key = os.environ.get("GHES_APP_PRIVATE_KEY", "")
    app_slug = os.environ.get("GHES_APP_SLUG", "")
    ca_bundle = os.environ.get("GHES_SSL_CA_BUNDLE")

    if not app_id or not private_key:
        print("ERROR: GHES_APP_ID and GHES_APP_PRIVATE_KEY must be set",
              file=sys.stderr)
        sys.exit(1)

    # -- Authenticate ----------------------------------------------------------
    print("[HawkEye] Generating GitHub App JWT...")
    jwt = generate_github_app_jwt(app_id, None, private_key)

    print(f"[HawkEye] Looking up installation for '{owner}'...")
    installation_id = find_installation_id(api_url, jwt, owner, ca_bundle)
    if not installation_id:
        print(f"ERROR: GitHub App is not installed on '{owner}'. "
              f"Ask an org admin to install the app.", file=sys.stderr)
        sys.exit(1)

    token, _ = get_installation_token(jwt, api_url, installation_id, ca_bundle=ca_bundle)
    print("[HawkEye] Got installation token")

    # -- Find PR ---------------------------------------------------------------
    pr_number = find_pr_for_branch(api_url, token, owner, repo, branch, ca_bundle)
    if not pr_number:
        print(f"[HawkEye] No open PR found for branch '{branch}' — skipping review")
        sys.exit(0)

    print(f"[HawkEye] Found PR #{pr_number}")

    # -- Placeholder + reviewer ------------------------------------------------
    placeholder_id = 0
    try:
        placeholder_id = post_placeholder_comment(
            api_url, token, owner, repo, pr_number, ca_bundle,
        )
    except Exception as exc:
        print(f"WARNING: Could not post placeholder comment: {exc}", file=sys.stderr)

    bot_login = f"{app_slug}[bot]" if app_slug else ""
    if bot_login:
        request_self_as_reviewer(
            api_url, token, owner, repo, pr_number, bot_login, ca_bundle,
        )

    # -- Resolve Claude credentials --------------------------------------------
    claude_url, claude_token = resolve_claude_credentials(
        api_url, token, owner, repo, ca_bundle,
    )
    if not claude_url or not claude_token:
        msg = (
            "No Claude API credentials found for this repo. Set "
            "`HAWKEYE_CLAUDE_API_URL` and `HAWKEYE_CLAUDE_BLOB` in your "
            "CircleCI project environment variables."
        )
        print(f"ERROR: {msg}", file=sys.stderr)
        update_placeholder_error(
            api_url, token, owner, repo, placeholder_id, ca_bundle, msg,
        )
        sys.exit(1)

    # -- Run review ------------------------------------------------------------
    print(f"[HawkEye] Starting review for {owner}/{repo}#{pr_number}")
    script_path = os.path.join(os.path.dirname(__file__), "hawkeye_pr_review.py")

    env = {
        **os.environ,
        "GITHUB_TOKEN": token,
        "GITHUB_API_URL": api_url,
        "CLAUDE_API_URL": claude_url,
        "CLAUDE_API_TOKEN": claude_token,
        "PLACEHOLDER_COMMENT_ID": str(placeholder_id),
    }
    ca_path = _ca_bundle_path(ca_bundle)
    if ca_path:
        env["SSL_CERT_FILE"] = ca_path
        env["REQUESTS_CA_BUNDLE"] = ca_path

    try:
        result = subprocess.run(
            [sys.executable, script_path, owner, repo, str(pr_number)],
            env=env,
            timeout=900,
        )
        if result.returncode == 0:
            print(f"[HawkEye] Review complete for {owner}/{repo}#{pr_number}")
        else:
            print(f"[HawkEye] Review exited with code {result.returncode}",
                  file=sys.stderr)
            update_placeholder_error(
                api_url, token, owner, repo, placeholder_id, ca_bundle,
                f"The review script exited with code `{result.returncode}`.",
            )
        sys.exit(result.returncode)
    except subprocess.TimeoutExpired:
        print("[HawkEye] Review timed out after 15 minutes", file=sys.stderr)
        update_placeholder_error(
            api_url, token, owner, repo, placeholder_id, ca_bundle,
            "The review timed out after 15 minutes.",
        )
        sys.exit(1)
    except Exception as exc:
        print(f"[HawkEye] Unexpected error: {exc}", file=sys.stderr)
        update_placeholder_error(
            api_url, token, owner, repo, placeholder_id, ca_bundle,
            f"Unexpected error: `{exc}`.",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
