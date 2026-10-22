#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements.txt
.venv/bin/python -m playwright install chromium
mkdir -p logs

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Arquivo .env criado. Preencha as credenciais de e-mail antes de ativar o monitor."
else
  chmod 600 .env
  echo "Arquivo .env existente preservado."
fi

echo "Instalação concluída."
echo "1. Edite: $PROJECT_DIR/.env"
echo "2. Teste: $PROJECT_DIR/.venv/bin/python $PROJECT_DIR/monitor.py --dry-run"
echo "3. Teste o e-mail: $PROJECT_DIR/.venv/bin/python $PROJECT_DIR/monitor.py --test-email"
echo "4. Ative: $PROJECT_DIR/enable.sh"
