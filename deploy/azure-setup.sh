#!/usr/bin/env bash
# deploy/azure-setup.sh
#
# One-time setup: provisions an Azure App Service and deploys the webhook server.
# Run this from the repo root:
#   chmod +x deploy/azure-setup.sh
#   ./deploy/azure-setup.sh
#
# Prerequisites:
#   - Azure CLI installed and logged in (az login)
#   - All variables below filled in

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — fill these in before running
# ---------------------------------------------------------------------------

SUBSCRIPTION_ID="your-azure-subscription-id"
RESOURCE_GROUP="claude-pr-reviewer"
LOCATION="westeurope"           # Change to your preferred Azure region
APP_NAME="claude-pr-reviewer"  # Must be globally unique — becomes <APP_NAME>.azurewebsites.net
APP_PLAN="claude-pr-reviewer-plan"

# GitHub App credentials (for production — org-wide GitHub App)
GITHUB_APP_ID="your-github-app-id"
GITHUB_APP_PRIVATE_KEY_PATH="./secrets/app.pem"  # Local path to the .pem file

# Webhook secret (any random string — must match what you set in GitHub webhook settings)
WEBHOOK_SECRET="$(openssl rand -hex 32)"

# Server RSA key pair — used to decrypt per-repo Claude credentials stored as
# GitHub Actions variables.  Generated once per deployment and never changes.
# Teams run scripts/encrypt_token.py to encrypt their token with the public key.
SERVER_PRIVATE_KEY="$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 2>/dev/null)"

# Claude / Bedrock credentials (server-wide fallback).
# If every repo uses per-repo credentials (CLAUDE_REVIEWER_API_URL /
# CLAUDE_REVIEWER_API_TOKEN Actions variables), these can be left as empty
# strings — the server will still start and use per-repo credentials.
CLAUDE_API_URL="your-bedrock-converse-endpoint"   # or "" if using per-repo creds only
CLAUDE_API_TOKEN="Bearer your-bedrock-token"       # or "" if using per-repo creds only

# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------

echo "==> Setting subscription"
az account set --subscription "$SUBSCRIPTION_ID"

echo "==> Creating resource group: $RESOURCE_GROUP"
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"

echo "==> Creating App Service plan: $APP_PLAN (B1 Linux)"
az appservice plan create \
  --name "$APP_PLAN" \
  --resource-group "$RESOURCE_GROUP" \
  --sku B1 \
  --is-linux

echo "==> Creating web app: $APP_NAME"
az webapp create \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --plan "$APP_PLAN" \
  --runtime "PYTHON:3.12"

echo "==> Setting startup command"
az webapp config set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --startup-file "python scripts/webhook_server.py"

echo "==> Uploading GitHub App private key as app setting"
PRIVATE_KEY_CONTENTS="$(cat "$GITHUB_APP_PRIVATE_KEY_PATH")"

echo "==> Configuring environment variables"
az webapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings \
    GITHUB_APP_ID="$GITHUB_APP_ID" \
    GITHUB_APP_PRIVATE_KEY="$PRIVATE_KEY_CONTENTS" \
    WEBHOOK_SECRET="$WEBHOOK_SECRET" \
    SERVER_PRIVATE_KEY="$SERVER_PRIVATE_KEY" \
    CLAUDE_API_URL="$CLAUDE_API_URL" \
    CLAUDE_API_TOKEN="$CLAUDE_API_TOKEN" \
    SCM_DO_BUILD_DURING_DEPLOYMENT="false"

echo "==> Deploying code"
# Package only the scripts directory
zip -r /tmp/claude-pr-reviewer-deploy.zip scripts/
az webapp deployment source config-zip \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --src /tmp/claude-pr-reviewer-deploy.zip
rm /tmp/claude-pr-reviewer-deploy.zip

echo ""
echo "============================================================"
echo "Deployment complete!"
echo ""
echo "App URL    : https://${APP_NAME}.azurewebsites.net"
echo "Health     : https://${APP_NAME}.azurewebsites.net/health"
echo "Webhook URL: https://${APP_NAME}.azurewebsites.net/webhook"
echo "Public key : https://${APP_NAME}.azurewebsites.net/public-key"
echo ""
echo "IMPORTANT — save this webhook secret (set it in GitHub webhook settings):"
echo "  WEBHOOK_SECRET=$WEBHOOK_SECRET"
echo ""
echo "Next steps:"
echo "  1. Set the webhook URL on the GitHub App:"
echo "     github.com/settings/apps/<your-app-slug>/advanced"
echo "     Webhook URL    : https://${APP_NAME}.azurewebsites.net/webhook"
echo "     Webhook secret : the WEBHOOK_SECRET printed above"
echo "     Events         : Pull requests, Issue comments"
echo ""
echo "  2. Users enroll by installing the GitHub App on their repo:"
echo "     github.com/apps/<your-app-slug> -> Install -> select repo"
echo ""
echo "  3. Each team configures their own Claude credentials:"
echo "     python3 scripts/encrypt_token.py \\"
echo "       --server https://${APP_NAME}.azurewebsites.net \\"
echo "       --token \"Bearer YOUR_BEDROCK_TOKEN\""
echo "     Then set in the repo → Settings → Variables → Actions:"
echo "       CLAUDE_REVIEWER_API_URL  = your Bedrock endpoint URL"
echo "       CLAUDE_REVIEWER_API_TOKEN = encrypted blob from above"
echo "============================================================"
