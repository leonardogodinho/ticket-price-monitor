#!/bin/zsh
# Envia as variáveis do .env local para os GitHub Secrets do repositório.
# Uso:
#   ./push-secrets.sh                      # usa o repo do diretório atual (origin)
#   ./push-secrets.sh usuario/repositorio  # alvo explícito
#
# Requisitos: gh (GitHub CLI) autenticado na conta correta.
# O .env NUNCA é commitado; este script apenas transfere os valores para o GitHub.
set -euo pipefail

PROJECT_DIR="${0:A:h}"
ENV_FILE="$PROJECT_DIR/.env"
REPO="${1:-}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Arquivo .env não encontrado em $ENV_FILE" >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) não encontrado. Instale com: brew install gh" >&2
  exit 1
fi

REPO_ARGS=()
if [[ -n "$REPO" ]]; then
  REPO_ARGS=(--repo "$REPO")
fi

echo "Conta gh ativa:"
gh auth status 2>&1 | grep -E "Active account|account" | head -4 || true
echo

count=0
while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  line="${raw_line#"${raw_line%%[![:space:]]*}"}" # ltrim
  [[ -z "$line" || "$line" == \#* || "$line" != *"="* ]] && continue
  line="${line#export }"

  key="${line%%=*}"
  value="${line#*=}"
  key="${key//[[:space:]]/}"

  # Remove aspas simples ou duplas que envolvam o valor inteiro.
  if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
    value="${value:1:${#value}-2}"
  fi

  [[ -z "$key" ]] && continue

  printf 'Definindo secret: %s\n' "$key"
  # Passa o valor como argumento (--body "$value") em vez de stdin, para não disputar
  # o stdin com o loop `read` (o que pode deslocar valores entre as chaves).
  gh secret set "$key" "${REPO_ARGS[@]}" --body "$value" </dev/null
  count=$((count + 1))
done < "$ENV_FILE"

echo
echo "$count secret(s) enviado(s)."
echo "Confira em: Settings > Secrets and variables > Actions do repositório."
