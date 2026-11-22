#!/usr/bin/env bash
# git diff exit convention: 0 same image inputs, 1 changed, >1 error.
# Keep these paths aligned with the Docker build inputs and publication workflow.
set -euo pipefail
git diff --quiet "$1" "$2" -- \
  .github/workflows/container.yml .github/scripts/image-inputs.sh \
  .dockerignore Dockerfile LICENSE go.mod go.sum cmd/ internal/
