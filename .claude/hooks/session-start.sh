#!/bin/bash
# Claude Code en la web: deja listo el entorno (TeX Live + dependencias de Python) para
# correr el pipeline y el smoke test. Idempotente: si ya está instalado, no hace nada.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# TeX Live con beamer, metropolis, algorithm2e, booktabs y tikz (ver README)
if ! command -v pdflatex >/dev/null || ! kpsewhich beamerthememetropolis.sty algorithm2e.sty >/dev/null; then
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq --no-install-recommends \
    texlive-latex-recommended texlive-latex-extra texlive-pictures \
    texlive-science texlive-fonts-recommended poppler-utils >/dev/null
fi

python3 -m pip install -q --root-user-action=ignore -r requirements.txt
