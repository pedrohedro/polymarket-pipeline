#!/usr/bin/env bash
#
# Garante que a Codex CLI esteja instalada e autenticada (via ChatGPT OAuth).
# Não usa API key — apenas a assinatura do ChatGPT.
#
# Uso: bash scripts/codex_auth.sh
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

# 1. Instala a CLI do Codex se não existir
if ! command -v codex >/dev/null 2>&1; then
    echo -e "${YELLOW}Codex CLI não encontrada. Instalando via npm...${NC}"
    if ! command -v npm >/dev/null 2>&1; then
        echo -e "${RED}npm não encontrado. Instale o Node.js (https://nodejs.org) e rode de novo.${NC}"
        exit 1
    fi
    npm install -g @openai/codex
fi

echo -e "${GREEN}✓${NC} Codex CLI: $(codex --version 2>/dev/null | head -1)"

# 2. Autentica se ainda não estiver logado
if codex login status >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Codex já autenticado (ChatGPT OAuth)."
    exit 0
fi

echo -e "${YELLOW}Autenticando no Codex com sua conta ChatGPT (OAuth, sem API key)...${NC}"

# Tenta login pelo navegador; se falhar (ambiente headless), usa device-auth
if codex login; then
    :
else
    echo -e "${YELLOW}Login pelo navegador falhou — tentando por código de dispositivo...${NC}"
    codex login --device-auth
fi

# Confirma
if codex login status >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Autenticação concluída."
else
    echo -e "${RED}Falha na autenticação do Codex. Rode 'codex login' manualmente.${NC}"
    exit 1
fi
