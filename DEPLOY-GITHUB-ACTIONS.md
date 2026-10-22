# Rodar 24/7 no GitHub Actions

Este guia deixa o monitor rodando na nuvem a cada ~5 minutos, sem depender do seu Mac
ficar ligado. O código lê toda a configuração de variáveis de ambiente, então na nuvem
usamos **GitHub Secrets** no lugar do arquivo `.env` (que continua local e ignorado pelo git).

## Visão geral

- Workflow: `.github/workflows/monitor.yml` (cron `*/5 * * * *` + execução manual).
- Ambiente: imagem oficial do Playwright (`mcr.microsoft.com/playwright/python`), que já
  traz o Chromium e as dependências de sistema.
- Estado: `.monitor-state.json` é persistido entre execuções via `actions/cache`, para não
  reenviar o mesmo alerta a cada queda.
- Segredos: enviados do seu `.env` para o GitHub via `./push-secrets.sh`.

## Pré-requisitos

- GitHub CLI (`gh`) autenticado na conta pessoal.
  - Confira a conta ativa: `gh auth status`
  - Para trocar a conta ativa: `gh auth switch`

## Passo a passo

### 1. Inicialize o git e faça o primeiro commit

```zsh
git init
git add .
git commit -m "Monitor de preço de ingresso com execução no GitHub Actions"
```

O `.gitignore` já impede o commit de `.env`, `.monitor-state.json`, `logs/` e `.venv/`.
Confirme que o `.env` NÃO aparece em `git status` antes de prosseguir.

### 2. Crie o repositório pessoal e faça push

Repositório privado é recomendado (o workflow em si não tem segredos, mas é boa prática):

```zsh
gh repo create ticket-price-monitor --private --source=. --remote=origin --push
```

Se tiver mais de uma conta no `gh`, garanta que a pessoal está ativa com `gh auth switch`
antes deste comando.

### 3. Envie os segredos

```zsh
./push-secrets.sh
```

Isso lê o seu `.env` e cria um GitHub Secret para cada variável. Nada sensível vai para
o repositório — apenas para o cofre de secrets do GitHub.

### 4. Teste o envio de e-mail pela nuvem

No GitHub: aba **Actions** > workflow **Monitor de preço do ingresso** > **Run workflow** >
marque **"Enviar apenas e-mail de teste"** > **Run**. Confirme que o e-mail chega.

### 5. Deixe rodando

Depois do teste, o cron assume sozinho e verifica o preço a cada ~5 minutos. Acompanhe
o histórico e os logs de cada execução na aba **Actions**.

## Observações importantes

- **Intervalo não é exato.** O cron do GitHub Actions pode atrasar alguns minutos em
  horários de pico. Para monitorar preço de ingresso isso costuma ser suficiente.
- **Entrega de e-mail.** Com `RESEND_FROM = onboarding@resend.dev`, o Resend só entrega
  para o e-mail dono da conta Resend. Para produção, verifique um domínio seu no Resend.
- **Rotacione a RESEND_API_KEY.** Se a chave já foi exposta, gere uma nova em
  https://resend.com/api-keys, revogue a antiga, atualize o `.env` e rode `./push-secrets.sh`
  novamente.
- **Atualizar um segredo depois:** edite o `.env` e rode `./push-secrets.sh` de novo, ou use
  `gh secret set NOME`.
