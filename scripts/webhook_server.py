#!/usr/bin/env python3
"""Standalone webhook server for Claude PR Reviewer.

Receives GitHub webhooks and triggers claude_pr_review.py, replacing the need
for GitHub Actions. Supports multiple GitHub environments (github.com orgs and
GitHub Enterprise Server) from a single process.

Zero pip dependencies — uses only the Python standard library + openssl binary
(available on any Linux/Mac system).

Usage:
  # Single-environment mode (env vars):
  export GITHUB_APP_ID=12345
  export GITHUB_APP_PRIVATE_KEY_PATH=/secrets/app.pem
  export WEBHOOK_SECRET=mysecret
  export CLAUDE_API_URL=https://...
  export CLAUDE_API_TOKEN=Bearer ...
  python3 scripts/webhook_server.py

  # Multi-environment mode (config file):
  export CONFIG_FILE=/config/config.json
  python3 scripts/webhook_server.py

  # Auth smoke test (prints token expiry, no review triggered):
  python3 scripts/webhook_server.py --test-auth

See config.example.json for the full configuration schema.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(message)s",
)
logger = logging.getLogger("webhook_server")


def _log(level: str, env_name: str, repo_context: str, msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    env_tag = f"[ENV:{env_name}]" if env_name else ""
    repo_tag = f"[{repo_context}]" if repo_context else ""
    getattr(logger, level)(f"[{ts}]{env_tag}{repo_tag} {msg}")


def info(msg: str, env: str = "", repo: str = "") -> None:
    _log("info", env, repo, msg)


def error(msg: str, env: str = "", repo: str = "") -> None:
    _log("error", env, repo, msg)


def warn(msg: str, env: str = "", repo: str = "") -> None:
    _log("warning", env, repo, msg)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8080
DEFAULT_HOST = "0.0.0.0"
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "claude_pr_review.py")


def _load_single_env_config() -> dict[str, Any]:
    """Build a single-environment config from flat env vars.

    PAT mode: set GITHUB_TOKEN instead of GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH.
    App mode: set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH (production).
    """
    using_pat = bool(os.environ.get("GITHUB_TOKEN"))
    # When SERVER_PRIVATE_KEY is set, per-repo GitHub variables supply Claude
    # credentials, so a global CLAUDE_API_URL/CLAUDE_API_TOKEN is optional
    # (they become a server-wide fallback rather than a hard requirement).
    has_per_repo_creds = bool(os.environ.get("SERVER_PRIVATE_KEY"))
    if using_pat:
        required: tuple[str, ...] = ("WEBHOOK_SECRET",)
        if not has_per_repo_creds:
            required += ("CLAUDE_API_URL", "CLAUDE_API_TOKEN")
    else:
        required = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "WEBHOOK_SECRET")
        if not has_per_repo_creds:
            required += ("CLAUDE_API_URL", "CLAUDE_API_TOKEN")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise ValueError(f"Missing required env vars for single-env mode: {', '.join(missing)}")
    return {
        "github_api_url": os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        "github_app_id": os.environ.get("GITHUB_APP_ID"),
        "github_app_private_key_path": os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH"),
        "webhook_secret": os.environ["WEBHOOK_SECRET"],
        "claude_api_url": os.environ["CLAUDE_API_URL"],
        "claude_api_token": os.environ["CLAUDE_API_TOKEN"],
        "ssl_ca_bundle": os.environ.get("SSL_CA_BUNDLE"),
    }


def load_config() -> dict[str, Any]:
    """
    Load server configuration.

    Returns a dict with top-level keys:
      port, host, script_path, max_concurrent_reviews, envs, single_env

    'envs' is a dict mapping env_name -> env_config.
    'single_env' is True when no CONFIG_FILE is set (all webhooks go to /webhook).
    """
    config_file = os.environ.get("CONFIG_FILE")
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    host = os.environ.get("HOST", DEFAULT_HOST)
    script_path = os.environ.get("SCRIPT_PATH", DEFAULT_SCRIPT_PATH)
    max_concurrent = int(os.environ.get("MAX_CONCURRENT_REVIEWS", DEFAULT_MAX_CONCURRENT))

    if config_file:
        with open(config_file) as f:
            raw = json.load(f)
        envs = raw.get("envs", {})
        if not envs:
            raise ValueError("CONFIG_FILE must have a non-empty 'envs' object")
        # Fill in ssl_ca_bundle default for each env
        for env_cfg in envs.values():
            env_cfg.setdefault("ssl_ca_bundle", os.environ.get("SSL_CA_BUNDLE"))
        return {
            "port": raw.get("port", port),
            "host": raw.get("host", host),
            "script_path": raw.get("script_path", script_path),
            "max_concurrent_reviews": raw.get("max_concurrent_reviews", max_concurrent),
            "envs": envs,
            "single_env": False,
        }
    else:
        env_cfg = _load_single_env_config()
        return {
            "port": port,
            "host": host,
            "script_path": script_path,
            "max_concurrent_reviews": max_concurrent,
            "envs": {"default": env_cfg},
            "single_env": True,
        }


# ---------------------------------------------------------------------------
# GitHub App JWT + Installation Token
# ---------------------------------------------------------------------------

_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_token_cache_lock = threading.Lock()


def _b64url(data: bytes | dict) -> str:
    if isinstance(data, dict):
        data = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_github_app_jwt(app_id: str, pem_path: str | None) -> str:
    """Generate a GitHub App JWT using RS256 via openssl subprocess.

    The private key is read from pem_path if provided, otherwise from the
    GITHUB_APP_PRIVATE_KEY environment variable (PEM contents as a string).
    """
    now = int(time.time())
    header = _b64url({"alg": "RS256", "typ": "JWT"})
    payload = _b64url({"iss": app_id, "iat": now - 60, "exp": now + 540})
    signing_input = f"{header}.{payload}"

    pem_contents = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if pem_contents:
        # Key provided as env var — write to a temp file for openssl
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
            f.write(pem_contents.replace("\\n", "\n"))
            tmp_path = f.name
        os.chmod(tmp_path, 0o600)
        try:
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", tmp_path, "-binary"],
                input=signing_input.encode(),
                capture_output=True,
            )
        finally:
            os.unlink(tmp_path)
    elif pem_path:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem_path, "-binary"],
            input=signing_input.encode(),
            capture_output=True,
        )
    else:
        raise RuntimeError(
            "No private key provided. Set GITHUB_APP_PRIVATE_KEY env var "
            "or github_app_private_key_path in config."
        )

    if result.returncode != 0:
        raise RuntimeError(f"openssl JWT signing failed: {result.stderr.decode().strip()}")

    sig = _b64url(result.stdout)
    return f"{signing_input}.{sig}"


def _github_request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    ca_bundle: str | None = None,
) -> dict:
    """Make a GitHub API request with Bearer token auth."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    ctx = None
    if ca_bundle:
        ctx = ssl.create_default_context(cafile=ca_bundle)

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"GitHub API {method} {url} → {e.code}: {body}") from e


def get_installation_token(
    jwt: str,
    github_api_url: str,
    installation_id: int,
    ca_bundle: str | None = None,
) -> tuple[str, float]:
    """
    Exchange a GitHub App JWT for an installation access token.
    Returns (token_string, unix_timestamp_expiry).
    """
    resp = _github_request(
        "POST",
        f"{github_api_url}/app/installations/{installation_id}/access_tokens",
        jwt,
        ca_bundle=ca_bundle,
    )
    token = resp["token"]
    expires_str = resp.get("expires_at", "")
    try:
        dt = datetime.strptime(expires_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        expires_at = dt.timestamp()
    except (ValueError, TypeError):
        expires_at = time.time() + 3600
    return token, expires_at


def get_cached_installation_token(
    env_name: str,
    env_cfg: dict,
    installation_id: int | None,
) -> str:
    """Return a cached installation token, refreshing if within 2 min of expiry.

    installation_id comes from the webhook payload (payload["installation"]["id"]).
    If GITHUB_TOKEN is set, it is used directly (PAT mode for local testing).
    """
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]

    if not installation_id:
        raise RuntimeError(
            "No installation_id in webhook payload and no GITHUB_TOKEN set. "
            "Ensure the webhook is configured on the GitHub App (not org settings)."
        )

    key = (env_name, installation_id)
    with _token_cache_lock:
        cached = _token_cache.get(key)
        if cached and time.time() < cached[1] - 120:
            return cached[0]

    jwt = generate_github_app_jwt(
        env_cfg["github_app_id"],
        env_cfg["github_app_private_key_path"],
    )
    token, expires_at = get_installation_token(
        jwt,
        env_cfg["github_api_url"],
        installation_id,
        ca_bundle=env_cfg.get("ssl_ca_bundle"),
    )
    with _token_cache_lock:
        _token_cache[key] = (token, expires_at)
    return token


# ---------------------------------------------------------------------------
# HMAC signature validation
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, secret: str, sig_header: str) -> bool:
    """Validate X-Hub-Signature-256 header."""
    if not sig_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ---------------------------------------------------------------------------
# GitHub API helpers (placeholder comment + commit status)
# ---------------------------------------------------------------------------

def post_placeholder_comment(
    github_api_url: str,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    ca_bundle: str | None,
) -> int:
    """Post a 'Reviewing…' placeholder comment and return its ID."""
    url = f"{github_api_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    # Derive avatar base URL the same way claude_pr_review.py does
    github_base = github_api_url.replace("/api/v3", "").replace("api.", "")
    avatar = f"{github_base}/anthropics.png?size=36"
    body = (
        f'<h2><img src="{avatar}" width="18" height="18" align="absmiddle"> '
        f"Reviewing your PR...</h2>\n\n"
        f"\u23f3 Claude is analyzing your changes. "
        f"A detailed review with inline comments will appear here shortly."
    )
    resp = _github_request("POST", url, token, {"body": body}, ca_bundle=ca_bundle)
    return resp["id"]


def post_pending_status(
    github_api_url: str,
    token: str,
    owner: str,
    repo: str,
    sha: str,
    ca_bundle: str | None,
) -> None:
    """Set commit status to pending (fast path for synchronize events)."""
    url = f"{github_api_url}/repos/{owner}/{repo}/statuses/{sha}"
    _github_request("POST", url, token, {
        "state": "pending",
        "context": "Claude Bedrock PR Review",
        "description": "Type /claude-review to review latest changes",
    }, ca_bundle=ca_bundle)


# ---------------------------------------------------------------------------
# Server key pair  (RSA-4096, generated once by the server admin)
# ---------------------------------------------------------------------------

def get_server_public_key_pem() -> str:
    """Derive the PEM public key from SERVER_PRIVATE_KEY env var using openssl."""
    private_key_pem = os.environ.get("SERVER_PRIVATE_KEY", "")
    if not private_key_pem:
        return ""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(private_key_pem.replace("\\n", "\n"))
        tmp = f.name
    os.chmod(tmp, 0o600)
    try:
        result = subprocess.run(
            ["openssl", "pkey", "-in", tmp, "-pubout"],
            capture_output=True,
        )
        return result.stdout.decode() if result.returncode == 0 else ""
    finally:
        os.unlink(tmp)


def decrypt_repo_token(encrypted_blob: str) -> str:
    """Decrypt a token blob produced by encrypt_token.py.

    Format: base64(RSA-OAEP(aes_key || iv)) + "." + base64(AES-256-CBC(token))
    """
    private_key_pem = os.environ.get("SERVER_PRIVATE_KEY", "").replace("\\n", "\n")
    if not private_key_pem:
        raise RuntimeError("SERVER_PRIVATE_KEY env var not set")

    parts = encrypted_blob.strip().split(".")
    if len(parts) != 2:
        raise RuntimeError("Invalid encrypted blob format (expected two base64 parts)")

    encrypted_key_iv = base64.b64decode(parts[0])
    encrypted_token = base64.b64decode(parts[1])

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(private_key_pem)
        key_path = f.name
    os.chmod(key_path, 0o600)
    try:
        # Decrypt the AES key+IV with RSA-OAEP
        result = subprocess.run(
            ["openssl", "pkeyutl", "-decrypt", "-inkey", key_path,
             "-pkeyopt", "rsa_padding_mode:oaep"],
            input=encrypted_key_iv,
            capture_output=True,
        )
    finally:
        os.unlink(key_path)

    if result.returncode != 0:
        raise RuntimeError(f"RSA decryption failed: {result.stderr.decode().strip()}")

    key_iv = result.stdout
    if len(key_iv) != 48:
        raise RuntimeError(f"Unexpected key+IV length: {len(key_iv)}")

    aes_key = key_iv[:32].hex()
    aes_iv = key_iv[32:].hex()

    # Decrypt the token with AES-256-CBC
    result2 = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc",
         "-K", aes_key, "-iv", aes_iv, "-nosalt"],
        input=encrypted_token,
        capture_output=True,
    )
    if result2.returncode != 0:
        raise RuntimeError(f"AES decryption failed: {result2.stderr.decode().strip()}")

    return result2.stdout.decode().strip()


# ---------------------------------------------------------------------------
# GitHub repo variables  (CLAUDE_REVIEWER_API_URL + CLAUDE_REVIEWER_API_TOKEN)
# ---------------------------------------------------------------------------

_var_cache: dict[tuple[str, str, str], tuple[dict, float]] = {}
_var_cache_lock = threading.Lock()
_VAR_CACHE_TTL = 300  # 5 minutes


def read_repo_variables(
    github_api_url: str,
    token: str,
    owner: str,
    repo: str,
    ca_bundle: str | None,
) -> dict[str, str]:
    """Read CLAUDE_REVIEWER_API_URL and CLAUDE_REVIEWER_API_TOKEN variables
    from the repo via GitHub API. Returns {} if not set or on error.
    Requires the GitHub App to have 'variables: read' permission.
    """
    # Include github_api_url in the key so multi-env deployments with repos
    # that share the same owner/name across different GitHub servers don't
    # cross-contaminate each other's cached credentials.
    key = (github_api_url, owner.lower(), repo.lower())
    with _var_cache_lock:
        cached = _var_cache.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]

    result: dict[str, str] = {}
    for var_name in ("CLAUDE_REVIEWER_API_URL", "CLAUDE_REVIEWER_API_TOKEN"):
        url = f"{github_api_url}/repos/{owner}/{repo}/actions/variables/{var_name}"
        try:
            resp = _github_request("GET", url, token, ca_bundle=ca_bundle)
            result[var_name] = resp.get("value", "")
        except RuntimeError:
            pass  # Variable not set or no permission — skip

    with _var_cache_lock:
        _var_cache[key] = (result, time.time() + _VAR_CACHE_TTL)
    return result


# ---------------------------------------------------------------------------
# Review invocation
# ---------------------------------------------------------------------------

def _resolve_claude_credentials(
    env_cfg: dict,
    owner: str,
    repo: str,
    installation_token: str,
) -> tuple[str, str]:
    """Resolve Claude API URL and token for a given repo.

    Lookup order (most specific wins):
      1. GitHub repo Actions variables  CLAUDE_REVIEWER_API_URL + CLAUDE_REVIEWER_API_TOKEN
         (set by the repo team; token value must be encrypted with encrypt_token.py)
      2. Per-org env vars  CLAUDE_API_URL_<OWNER> / CLAUDE_API_TOKEN_<OWNER>
      3. Server default from env_cfg (CLAUDE_API_URL / CLAUDE_API_TOKEN)
    """
    # 1. Per-repo GitHub variables
    if os.environ.get("SERVER_PRIVATE_KEY"):
        try:
            repo_vars = read_repo_variables(
                env_cfg["github_api_url"],
                installation_token,
                owner,
                repo,
                env_cfg.get("ssl_ca_bundle"),
            )
            repo_url = repo_vars.get("CLAUDE_REVIEWER_API_URL", "").strip()
            repo_token_enc = repo_vars.get("CLAUDE_REVIEWER_API_TOKEN", "").strip()
            if repo_url and repo_token_enc:
                repo_token = decrypt_repo_token(repo_token_enc)
                return repo_url, repo_token
        except Exception as exc:
            warn(f"Could not read/decrypt repo variables for {owner}/{repo}: {exc}")

    # 2. Per-org env vars
    key = owner.upper().replace("-", "_").replace(".", "_")
    org_url = os.environ.get(f"CLAUDE_API_URL_{key}")
    org_token = os.environ.get(f"CLAUDE_API_TOKEN_{key}")
    if org_url and org_token:
        return org_url, org_token

    # 3. Server default
    return env_cfg.get("claude_api_url", ""), env_cfg.get("claude_api_token", "")


def invoke_review(
    env_name: str,
    env_cfg: dict,
    script_path: str,
    owner: str,
    repo: str,
    pr_number: int,
    placeholder_id: int,
    installation_token: str,
) -> None:
    """Run claude_pr_review.py as a subprocess for a single PR."""
    repo_ctx = f"{owner}/{repo}#{pr_number}"
    info("Starting review subprocess", env=env_name, repo=repo_ctx)
    claude_api_url, claude_api_token = _resolve_claude_credentials(
        env_cfg, owner, repo, installation_token
    )

    if not claude_api_url or not claude_api_token:
        error("No Claude credentials configured for this repo — skipping review", env=env_name, repo=repo_ctx)
        if placeholder_id:
            try:
                err_body = (
                    "<h2>⚠️ Claude PR Reviewer — credentials not configured</h2>\n\n"
                    "This repo has no Claude API credentials set up.\n\n"
                    "Ask your team admin to set the **`CLAUDE_REVIEWER_API_URL`** and "
                    "**`CLAUDE_REVIEWER_API_TOKEN`** Actions variables on this repo "
                    "(see the [setup guide](https://github.com/adobe-rnd/claude-pr-reviewer) "
                    "for instructions)."
                )
                _github_request(
                    "PATCH",
                    f"{env_cfg['github_api_url']}/repos/{owner}/{repo}/issues/comments/{placeholder_id}",
                    installation_token,
                    {"body": err_body},
                    ca_bundle=env_cfg.get("ssl_ca_bundle"),
                )
            except Exception as patch_exc:
                error(f"Failed to update placeholder with credentials error: {patch_exc}", env=env_name, repo=repo_ctx)
        return

    env = {
        **os.environ,
        "GITHUB_TOKEN": installation_token,
        "GITHUB_API_URL": env_cfg["github_api_url"],
        "CLAUDE_API_URL": claude_api_url,
        "CLAUDE_API_TOKEN": claude_api_token,
        "PLACEHOLDER_COMMENT_ID": str(placeholder_id),
    }
    if env_cfg.get("ssl_ca_bundle"):
        env["SSL_CERT_FILE"] = env_cfg["ssl_ca_bundle"]
        env["REQUESTS_CA_BUNDLE"] = env_cfg["ssl_ca_bundle"]

    try:
        result = subprocess.run(
            [sys.executable, script_path, owner, repo, str(pr_number)],
            env=env,
            timeout=600,
        )
        if result.returncode != 0:
            warn(f"Review subprocess exited {result.returncode}", env=env_name, repo=repo_ctx)
        else:
            info("Review complete", env=env_name, repo=repo_ctx)
    except subprocess.TimeoutExpired:
        error("Review subprocess timed out after 600s", env=env_name, repo=repo_ctx)
    except Exception as exc:
        error(f"Review subprocess error: {exc}", env=env_name, repo=repo_ctx)


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------

def dispatch_event(
    env_name: str,
    env_cfg: dict,
    script_path: str,
    event_type: str,
    payload: dict,
) -> None:
    """Process a validated webhook event.

    Opt-in is handled by GitHub: the webhook is configured on the GitHub App
    itself, so events are only delivered for repos where the app is installed.
    Installing the app on a repo = opting in.
    """
    repo_info = payload.get("repository", {})
    owner = repo_info.get("owner", {}).get("login", "")
    repo_name = repo_info.get("name", "")
    repo_ctx = f"{owner}/{repo_name}"
    installation_id = payload.get("installation", {}).get("id")

    try:
        if event_type == "pull_request":
            _handle_pull_request(env_name, env_cfg, script_path, payload,
                                 owner, repo_name, repo_ctx, installation_id)
        elif event_type == "issue_comment":
            _handle_issue_comment(env_name, env_cfg, script_path, payload,
                                  owner, repo_name, repo_ctx, installation_id)
        else:
            info(f"Ignoring event type '{event_type}'", env=env_name, repo=repo_ctx)
    except Exception as exc:
        error(f"dispatch_event error: {exc}", env=env_name, repo=repo_ctx)


def _handle_pull_request(
    env_name: str,
    env_cfg: dict,
    script_path: str,
    payload: dict,
    owner: str,
    repo: str,
    repo_ctx: str,
    installation_id: int | None,
) -> None:
    action = payload.get("action", "")
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    sha = pr.get("head", {}).get("sha", "")
    is_draft = pr.get("draft", False)

    if action == "synchronize":
        info(f"PR #{pr_number} synchronize — setting pending status", env=env_name, repo=repo_ctx)
        token = get_cached_installation_token(env_name, env_cfg, installation_id)
        post_pending_status(env_cfg["github_api_url"], token, owner, repo, sha,
                            env_cfg.get("ssl_ca_bundle"))
        return

    if action not in ("opened", "reopened", "ready_for_review"):
        info(f"Ignoring PR action '{action}'", env=env_name, repo=repo_ctx)
        return

    if is_draft and action != "ready_for_review":
        info(f"Skipping draft PR #{pr_number}", env=env_name, repo=repo_ctx)
        return

    info(f"PR #{pr_number} {action} — triggering review", env=env_name, repo=repo_ctx)
    token = get_cached_installation_token(env_name, env_cfg, installation_id)
    placeholder_id = post_placeholder_comment(
        env_cfg["github_api_url"], token, owner, repo, pr_number,
        env_cfg.get("ssl_ca_bundle"),
    )
    invoke_review(env_name, env_cfg, script_path, owner, repo, pr_number, placeholder_id, token)


def _handle_issue_comment(
    env_name: str,
    env_cfg: dict,
    script_path: str,
    payload: dict,
    owner: str,
    repo: str,
    repo_ctx: str,
    installation_id: int | None,
) -> None:
    action = payload.get("action", "")
    comment = payload.get("comment", {})
    issue = payload.get("issue", {})

    if action != "created":
        return
    if "/claude-review" not in comment.get("body", ""):
        return
    if "pull_request" not in issue:
        return  # Comment on issue, not a PR

    pr_number = issue.get("number")
    info(f"PR #{pr_number} /claude-review comment — triggering review", env=env_name, repo=repo_ctx)
    token = get_cached_installation_token(env_name, env_cfg, installation_id)
    placeholder_id = post_placeholder_comment(
        env_cfg["github_api_url"], token, owner, repo, pr_number,
        env_cfg.get("ssl_ca_bundle"),
    )
    invoke_review(env_name, env_cfg, script_path, owner, repo, pr_number, placeholder_id, token)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for GitHub webhooks."""

    # These are set on the class by the server factory
    server_config: dict
    executor: ThreadPoolExecutor

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN002
        # Suppress default BaseHTTPRequestHandler access log; we use our own
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health" or self.path.startswith("/health?"):
            self._health()
        elif self.path == "/public-key" or self.path.startswith("/public-key?"):
            self._public_key()
        else:
            self._respond(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        cfg = self.server_config

        # Resolve env config from URL path
        env_name, env_cfg = self._resolve_env(self.path, cfg)
        if env_cfg is None:
            self._respond(404, f"Unknown webhook path: {self.path}")
            return

        # Read body (cap at 25 MB)
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 25 * 1024 * 1024:
            self._respond(413, "Payload too large")
            return
        body = self.rfile.read(content_length)

        # Validate signature
        sig_header = self.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(body, env_cfg["webhook_secret"], sig_header):
            warn(f"Invalid signature for {self.path}", env=env_name)
            self._respond(401, "Invalid signature")
            return

        # Parse event
        event_type = self.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, "Invalid JSON")
            return

        # Ack immediately, dispatch in background
        self._respond(202, "Accepted")

        self.executor.submit(
            dispatch_event,
            env_name,
            env_cfg,
            cfg["script_path"],
            event_type,
            payload,
        )

    def _public_key(self) -> None:
        """GET /public-key — return the server's RSA public key in PEM format.

        Teams use this to encrypt their Claude API token with encrypt_token.py
        before storing it as a GitHub repo variable.
        """
        pem = get_server_public_key_pem()
        if not pem:
            self._respond(503, "SERVER_PRIVATE_KEY not configured on this server")
            return
        body = pem.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-pem-file")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve_env(
        self,
        path: str,
        cfg: dict,
    ) -> tuple[str, dict | None]:
        """Match URL path to an environment config."""
        clean = path.split("?")[0].rstrip("/")

        if cfg["single_env"]:
            # Single-env: accept any path starting with /webhook
            if clean.startswith("/webhook"):
                env_name = "default"
                return env_name, cfg["envs"][env_name]
            return "", None

        # Multi-env: /webhook/{env_name}
        for env_name, env_cfg in cfg["envs"].items():
            if clean == f"/webhook/{env_name}":
                return env_name, env_cfg
        return "", None

    def _health(self) -> None:
        body = json.dumps({
            "status": "ok",
            "envs": list(self.server_config["envs"].keys()),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, code: int, message: str) -> None:
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a new thread."""
    daemon_threads = True


# ---------------------------------------------------------------------------
# Auth smoke test
# ---------------------------------------------------------------------------

def run_test_auth(cfg: dict) -> None:
    """Test GitHub App JWT auth for all configured environments."""
    import sys

    all_ok = True
    for env_name, env_cfg in cfg["envs"].items():
        print(f"\n=== Testing auth for env: {env_name} ===")
        print(f"  GitHub API URL : {env_cfg['github_api_url']}")
        print(f"  App ID         : {env_cfg['github_app_id']}")
        print(f"  PEM path       : {env_cfg['github_app_private_key_path']}")
        try:
            jwt = generate_github_app_jwt(
                env_cfg["github_app_id"],
                env_cfg["github_app_private_key_path"],
            )
            print("  JWT generation : OK")
            # Try to list installations
            installations = _github_request(
                "GET",
                f"{env_cfg['github_api_url']}/app/installations",
                jwt,
                ca_bundle=env_cfg.get("ssl_ca_bundle"),
            )
            print(f"  Installations  : {[i.get('account', {}).get('login') for i in installations]}")
            print("  Auth test      : PASSED")
        except Exception as exc:
            print(f"  Auth test      : FAILED — {exc}")
            all_ok = False

    sys.exit(0 if all_ok else 1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if "--test-auth" in sys.argv:
        cfg = load_config()
        run_test_auth(cfg)
        return

    try:
        cfg = load_config()
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    script_path = cfg["script_path"]
    if not os.path.isfile(script_path):
        print(f"Review script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    executor = ThreadPoolExecutor(max_workers=cfg["max_concurrent_reviews"])

    # Attach config and executor to the handler class
    WebhookHandler.server_config = cfg
    WebhookHandler.executor = executor

    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), WebhookHandler)

    env_names = list(cfg["envs"].keys())
    mode = "single-env" if cfg["single_env"] else "multi-env"
    info(f"Webhook server starting ({mode})")
    info(f"Environments : {env_names}")
    info(f"Listening on : {cfg['host']}:{cfg['port']}")
    info(f"Script path  : {script_path}")
    if cfg["single_env"]:
        info("Webhook URL  : POST /webhook")
    else:
        for name in env_names:
            info(f"Webhook URL  : POST /webhook/{name}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        info("Shutting down...")
        executor.shutdown(wait=False)
        server.server_close()


if __name__ == "__main__":
    main()
