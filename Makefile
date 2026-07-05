# Polymarket Pipeline — atalhos de execução
#
# Exemplos:
#   make dev              # setup + auth + roda em DRY-RUN (sem dinheiro real)
#   make prod            # setup + auth + roda AO VIVO (--live, dinheiro real!)
#   make local           # roda offline com o motor heurístico (sem auth)
#   make dev ENGINE=auto  # troca o motor de classificação
#
# Variável ENGINE: codex (padrão) | local | anthropic | openai | auto

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
ENGINE ?= codex

# Só exige autenticação do Codex quando o motor for "codex"
ifeq ($(ENGINE),codex)
AUTH_DEP := auth
else
AUTH_DEP :=
endif

.PHONY: help setup auth verify dev prod local dashboard backtest trades stats clean

help:
	@echo ""
	@echo "  Polymarket Pipeline — comandos:"
	@echo ""
	@echo "    make setup      Cria venv, instala dependências e prepara o .env"
	@echo "    make auth       Instala/loga na Codex CLI (ChatGPT OAuth, sem API key)"
	@echo "    make verify     Checa credenciais e conexões"
	@echo ""
	@echo "    make dev        Setup + auth + opera em DRY-RUN (simulação, sem risco)"
	@echo "    make prod       Setup + auth + opera AO VIVO (--live, DINHEIRO REAL)"
	@echo "    make local      Opera offline com o motor heurístico (sem auth)"
	@echo ""
	@echo "    make dashboard  Dashboard ao vivo"
	@echo "    make backtest   Backtest da estratégia"
	@echo "    make trades     Lista as apostas registradas"
	@echo "    make stats      Estatísticas de desempenho"
	@echo "    make clean      Remove venv e banco de dados"
	@echo ""
	@echo "  Motor atual (ENGINE): $(ENGINE)   [codex|local|anthropic|openai|auto]"
	@echo ""

# --- Ambiente ---------------------------------------------------------------

$(PY):
	@echo ">> Criando ambiente virtual em $(VENV)..."
	@$(PYTHON) -m venv $(VENV) || { \
		echo "ERRO: falha ao criar venv. No Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }

setup: $(PY)
	@echo ">> Instalando dependências..."
	@$(PIP) install --upgrade pip -q
	@$(PIP) install -r requirements.txt -q
	@test -f .env || { cp .env.example .env && echo ">> .env criado a partir de .env.example"; }
	@echo ">> Setup completo."

auth:
	@bash scripts/codex_auth.sh

# --- Operação ---------------------------------------------------------------

verify: setup $(AUTH_DEP)
	@CLASSIFIER_ENGINE=$(ENGINE) $(PY) cli.py verify

dev: setup $(AUTH_DEP)
	@echo ">> Iniciando em DRY-RUN (motor: $(ENGINE)) — Ctrl+C para parar"
	@CLASSIFIER_ENGINE=$(ENGINE) $(PY) cli.py watch

prod: setup $(AUTH_DEP)
	@echo ""
	@echo "  ****************************************************************"
	@echo "  *  MODO AO VIVO — ORDENS REAIS COM DINHEIRO REAL (motor: $(ENGINE))"
	@echo "  ****************************************************************"
	@if [ "$(CONFIRM)" != "1" ]; then \
		read -p "  Digite 'operar' para confirmar: " ans; \
		if [ "$$ans" != "operar" ]; then echo "  Abortado."; exit 1; fi; \
	fi
	@CLASSIFIER_ENGINE=$(ENGINE) $(PY) cli.py watch --live

local: setup
	@echo ">> Iniciando OFFLINE (motor local) — Ctrl+C para parar"
	@CLASSIFIER_ENGINE=local $(PY) cli.py watch

dashboard: setup
	@CLASSIFIER_ENGINE=$(ENGINE) $(PY) cli.py dashboard

backtest: setup
	@CLASSIFIER_ENGINE=$(ENGINE) $(PY) cli.py backtest

trades: setup
	@$(PY) cli.py trades

stats: setup
	@$(PY) cli.py stats

clean:
	@rm -rf $(VENV) trades.db trades.db-shm trades.db-wal
	@echo ">> Removidos: $(VENV), trades.db"
