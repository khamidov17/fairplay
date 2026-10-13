#!/usr/bin/env bash
# Build the front-end assets for local development, the same steps the Dockerfile runs:
# npm packages, the rich-editor bundle, the compiled CSS, and the libraries served from
# resources/libs that come from npm (Monaco and Ace code editors, MathJax for math).
#
#   bash classroom/scripts/dev-assets.sh
#
# Needs Node 22 and the global sass/postcss tools (see docs/classroom/BASELINE.md).
set -euo pipefail

CLASSROOM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/node22/bin:$HOME/.npm-global/bin:$PATH"
cd "$CLASSROOM_DIR"

npm ci
npm run build:editor
./make_style.sh

rm -rf resources/libs/monaco resources/libs/mathjax resources/libs/ace
mkdir -p resources/libs/monaco resources/libs/mathjax resources/libs/ace
cp -R node_modules/monaco-editor/min/vs resources/libs/monaco/vs
cp -R node_modules/mathjax/es5 resources/libs/mathjax/es5
cp -R node_modules/ace-builds/src-min-noconflict/. resources/libs/ace/
echo "[classroom] assets built"
