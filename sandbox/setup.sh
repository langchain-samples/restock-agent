#!/usr/bin/env bash
# Bake step: install the Link CLI into the sandbox snapshot. Runs once per deploy.
set -euo pipefail

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Node.js and npm are required in the sandbox image; set docker_image to a node image." >&2
  exit 1
fi
node --version
npm install -g @stripe/link-cli@0.22.0
link-cli --version
mkdir -p /workspace
