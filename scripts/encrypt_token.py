#!/usr/bin/env python3
"""encrypt_token.py — encrypt a Claude API token for storage as a GitHub repo variable
or credentials file.

Run this locally. Nothing secret is ever sent to the webhook server.

Usage:
  python3 scripts/encrypt_token.py --token "YOUR_BEDROCK_TOKEN"

What it does:
  1. Reads the server's RSA public key from hawkeye-public.pem
  2. Generates a random AES-256 key + IV
  3. Encrypts the token with AES-256-CBC
  4. Encrypts the AES key+IV with RSA-4096-OAEP (using the server's public key)
  5. Outputs the encrypted blob to store as HAWKEYE_CLAUDE_BLOB

Zero pip dependencies — requires Python 3.8+ and openssl on PATH.

Option A — GitHub Actions Variables (repos with Actions enabled):
  1. Go to your repo → Settings → Secrets and variables → Actions → Variables
  2. Create HAWKEYE_CLAUDE_API_URL  = your Bedrock endpoint URL
  3. Create HAWKEYE_CLAUDE_BLOB = the encrypted blob printed by this script

Option B — credentials file (repos without GitHub Actions, e.g. GHES):
  1. Create .hawkeye/credentials in your repo with these two lines:
       HAWKEYE_CLAUDE_API_URL=<your Bedrock endpoint URL>
       HAWKEYE_CLAUDE_BLOB=<the encrypted blob printed by this script>
  2. Commit and push the file — it is safe to commit since the token is encrypted
     with the server's RSA public key and cannot be decrypted without the server's
     private key.
"""

import argparse
import base64
import os
import subprocess
import sys
import tempfile
import urllib.request


def fetch_public_key(server_url: str) -> str:
    """Download the server's RSA public key PEM."""
    url = server_url.rstrip("/") + "/public-key"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read().decode()
    except Exception as exc:
        print(f"ERROR: Could not fetch public key from {url}: {exc}", file=sys.stderr)
        sys.exit(1)


def encrypt_token(public_key_pem: str, token: str) -> str:
    """Hybrid-encrypt token: RSA-OAEP(AES key+IV) + AES-256-CBC(token).

    Returns a dot-separated base64 blob safe to store as a repo variable.
    """
    # Generate random 32-byte AES key + 16-byte IV
    aes_key = os.urandom(32)
    aes_iv = os.urandom(16)
    key_iv = aes_key + aes_iv

    # Write public key to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(public_key_pem)
        pub_path = f.name
    os.chmod(pub_path, 0o600)

    try:
        # Encrypt AES key+IV with RSA-OAEP
        result = subprocess.run(
            ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", pub_path,
             "-pkeyopt", "rsa_padding_mode:oaep"],
            input=key_iv,
            capture_output=True,
        )
    finally:
        os.unlink(pub_path)

    if result.returncode != 0:
        print(f"ERROR: RSA encryption failed: {result.stderr.decode().strip()}", file=sys.stderr)
        sys.exit(1)

    encrypted_key_iv = result.stdout

    # Encrypt token with AES-256-CBC
    result2 = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc",
         "-K", aes_key.hex(), "-iv", aes_iv.hex(), "-nosalt"],
        input=token.encode(),
        capture_output=True,
    )
    if result2.returncode != 0:
        print(f"ERROR: AES encryption failed: {result2.stderr.decode().strip()}", file=sys.stderr)
        sys.exit(1)

    encrypted_token = result2.stdout

    part1 = base64.b64encode(encrypted_key_iv).decode()
    part2 = base64.b64encode(encrypted_token).decode()
    return f"{part1}.{part2}"


DEFAULT_PUBLIC_KEY = os.path.join(os.path.dirname(__file__), "..", "hawkeye-public.pem")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Encrypt a Claude API token for secure storage as a GitHub repo variable.\n\n"
            "Example:\n"
            "  python3 encrypt_token.py --token \"YOUR_BEDROCK_API_KEY\"\n\n"
            "The encrypted blob is then stored as HAWKEYE_CLAUDE_BLOB in your repo's\n"
            "Settings → Secrets and variables → Actions → Variables."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--token",
        required=True,
        metavar="TOKEN",
        help=(
            "The Bedrock API key to encrypt. This is the raw API key used to\n"
            "authenticate requests to the Bedrock endpoint configured in\n"
            "HAWKEYE_CLAUDE_API_URL on your repo.\n"
            "Example: --token \"YOUR_BEDROCK_API_KEY\""
        ),
    )
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args()
    public_key_file = DEFAULT_PUBLIC_KEY

    with open(public_key_file) as f:
        public_key_pem = f.read()
    print(f"Using public key: {public_key_file}")

    if "PUBLIC KEY" not in public_key_pem:
        print("ERROR: Fetched content does not look like a PEM public key", file=sys.stderr)
        sys.exit(1)

    print("Encrypting token ...")
    blob = encrypt_token(public_key_pem, args.token)

    print()
    print("=" * 60)
    print("Encrypted blob (copy this as HAWKEYE_CLAUDE_BLOB):")
    print()
    print(blob)
    print()
    print("=" * 60)
    print()
    print("Next steps:")
    print()
    print("  Option A — GitHub Actions Variables (repos with Actions enabled):")
    print("    1. Go to your repo → Settings → Secrets and variables → Actions → Variables")
    print("    2. Create variable: HAWKEYE_CLAUDE_API_URL  = <your Bedrock endpoint URL>")
    print("    3. Create variable: HAWKEYE_CLAUDE_BLOB = <the blob above>")
    print()
    print("  Option B — credentials file (repos without GitHub Actions, e.g. GHES):")
    print("    1. Create .hawkeye/credentials in your repo with:")
    print("         HAWKEYE_CLAUDE_API_URL=<your Bedrock endpoint URL>")
    print("         HAWKEYE_CLAUDE_BLOB=<the blob above>")
    print("    2. Commit and push — safe to commit, token is encrypted.")
    print()
    print("The webhook server will pick up credentials automatically.")


if __name__ == "__main__":
    main()
