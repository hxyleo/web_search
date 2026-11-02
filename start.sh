#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077

if [ ! -f .crawl4ai.env ]; then
    crawl4ai_token=$(openssl rand -hex 32)
    printf 'CRAWL4AI_API_TOKEN=%s\n' "$crawl4ai_token" > .crawl4ai.env
fi
if [ ! -f .searxng.env ]; then
    searxng_secret=$(openssl rand -hex 32)
    printf 'SEARXNG_SECRET=%s\n' "$searxng_secret" > .searxng.env
fi

docker compose up -d "$@"
