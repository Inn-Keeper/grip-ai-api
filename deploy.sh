#!/usr/bin/env bash
#
# Deploys grip-ai-api to Cloud Run, building the Dockerfile in this directory.
#
#   ./deploy.sh https://your-app.vercel.app
#   ./deploy.sh "https://your-app.vercel.app,https://grip.example.com"
#
# One-time setup:
#
#   gcloud auth login
#   gcloud config set project <your-project-id>
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
#                          secretmanager.googleapis.com
#   printf %s "$GEMINI_API_KEY"    | gcloud secrets create gemini-api-key    --data-file=-
#   printf %s "$SUPABASE_ANON_KEY" | gcloud secrets create supabase-anon-key --data-file=-
#
# Rotating a key later is `gcloud secrets versions add <name> --data-file=-`,
# then redeploy. Secrets are never passed on this command line, so they stay
# out of shell history and out of `gcloud run services describe`.
#
# ponytail: a shell script rather than a service.yaml. A declarative config
# cannot be combined with --source, so it would mean building and pushing to
# Artifact Registry as separate steps for no gain at this size.
set -euo pipefail

ORIGINS="${1:?usage: ./deploy.sh https://your-app.vercel.app[,https://another]}"
: "${SUPABASE_URL:?set SUPABASE_URL to your Supabase project URL}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-grip-ai-api}"

# ^@^ sets @ as the delimiter, because ALLOWED_ORIGINS is itself a
# comma-separated list and the default comma delimiter would split it.
ENV_VARS="^@^APP_ENV=production"
ENV_VARS="$ENV_VARS@ALLOWED_ORIGINS=$ORIGINS"
ENV_VARS="$ENV_VARS@SUPABASE_URL=$SUPABASE_URL"
ENV_VARS="$ENV_VARS@AI_PROVIDER=gemini"
ENV_VARS="$ENV_VARS@AI_MODEL_GRADE=gemini-3.5-flash"
ENV_VARS="$ENV_VARS@AI_MAX_OUTPUT_TOKENS=8000"
ENV_VARS="$ENV_VARS@AI_REASONING_EFFORT=low"
ENV_VARS="$ENV_VARS@AI_TIMEOUT_SECONDS=30"
ENV_VARS="$ENV_VARS@AI_MAX_RETRIES=1"
ENV_VARS="$ENV_VARS@AI_CONTEXT_MAX_CHARS=24000"
ENV_VARS="$ENV_VARS@AI_GRADE_CACHE_SIZE=256"

# --min-instances 0: an always-warm instance bills CPU around the clock, which
#   leaves the always-free allowance. Cold starts cost a few seconds.
# --max-instances 1: the grade cache and the remembered rate limit both live in
#   the process (app/service.py). A second instance learns the daily quota
#   limit separately, spending one of the 20 free-tier requests to do it.
# --allow-unauthenticated: the browser authenticates with a Supabase token,
#   which the service validates itself; it has no Google identity to present.
# --timeout 60s: leaves headroom over AI_TIMEOUT_SECONDS=30.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 1 \
  --cpu 1 \
  --memory 512Mi \
  --timeout 60s \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,SUPABASE_ANON_KEY=supabase-anon-key:latest"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "Deployed: $URL"
echo "Check:    curl $URL/ready   # names any missing configuration"
echo "Then set VITE_AI_URL=$URL in Vercel and redeploy the web app."
