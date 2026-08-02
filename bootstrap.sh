#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${AIVA_REPO_URL:-https://github.com/banddude/aiva-bootstrap.git}"
BRANCH="${AIVA_BRANCH:-main}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git

git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP_DIR/aiva-bootstrap"
cd "$TMP_DIR/aiva-bootstrap"
exec ./install.sh "$@"
