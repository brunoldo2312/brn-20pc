#!/usr/bin/env bash
# ============================================================
#   iniciar.sh - BRN Node (Linux / macOS) - v3
# ------------------------------------------------------------
#   v3:
#     [FIX] NÃO sobrescreve brn_network.env — só cria se faltar
#     [FIX] NÃO sobrescreve MONGO_PASS/MONGO_URI do env
#     [FIX] NÃO exporta senhas hardcoded que sobrepõem o env
#     [NEW] Mostra a config final antes de subir
#     [NEW] Detecta modo (origin/client) automaticamente se não passar
#     [NEW] --no-autoconfig, --no-mongo, --no-verify continuam
#
#   Uso:
#     ./iniciar.sh                      # modo HUB/ORIGEM (padrao)
#     ./iniciar.sh --client             # modo CLIENTE
#     ./iniciar.sh --headless           # sem GUI
#     ./iniciar.sh --reset-identity     # apaga identidade
#     ./iniciar.sh --no-mongo           # nao testa Mongo
#     ./iniciar.sh --no-autoconfig      # pula auto-config
#     ./iniciar.sh --help               # ajuda
# ============================================================

set -u

# ------------------------------------------------------------
# Cores
# ------------------------------------------------------------
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[0;32m'
C_RED='\033[0;31m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'
C_CYAN='\033[0;36m'

info()  { echo -e "${C_BLUE}[i]${C_RESET} $*"; }
ok()    { echo -e "${C_GREEN}[ok]${C_RESET} $*"; }
warn()  { echo -e "${C_YELLOW}[!]${C_RESET} $*"; }
err()   { echo -e "${C_RED}[X]${C_RESET} $*"; }
die()   { err "$*"; exit 1; }

# ------------------------------------------------------------
# Ir para o diretório do script
# ------------------------------------------------------------
cd "$(dirname "$(readlink -f "$0")")" || die "Não consegui ir para o diretório do script"
BASE_DIR="$(pwd)"

echo ""
echo -e "${C_BOLD}============================================================${C_RESET}"
echo -e "${C_BOLD}  BRN Node - Linux/macOS${C_RESET}"
echo -e "${C_BOLD}============================================================${C_RESET}"
echo ""

# ------------------------------------------------------------
# Parse de argumentos
# ------------------------------------------------------------
MODE=""                 # "" = auto (detecta do env) | origin | client
HEADLESS=0
RESET_IDENTITY=0
SKIP_MONGO=0
SKIP_AUTOCONFIG=0
SKIP_VERIFY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --client)         MODE="client" ;;
        --hub|--origin)   MODE="origin" ;;
        --headless)       HEADLESS=1 ;;
        --reset-identity) RESET_IDENTITY=1 ;;
        --no-mongo)       SKIP_MONGO=1 ;;
        --no-autoconfig)  SKIP_AUTOCONFIG=1 ;;
        --no-verify)      SKIP_VERIFY=1 ;;
        --help|-h)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            warn "Argumento desconhecido: $1"
            ;;
    esac
    shift
done

# ------------------------------------------------------------
# Detectar Python (antes de tudo)
# ------------------------------------------------------------
PYTHON_BIN=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PYTHON_BIN="$cmd"
        break
    fi
done
[ -z "$PYTHON_BIN" ] && die "Python não encontrado no PATH"
PYVER=$("$PYTHON_BIN" --version 2>&1)
ok "$PYVER ($PYTHON_BIN)"
echo ""

# ------------------------------------------------------------
# 1) Carregar brn_network.env (SEM sobrescrever)
# ------------------------------------------------------------
if [ -f "brn_network.env" ]; then
    info "Carregando brn_network.env..."
    # remove \r caso tenha vindo do Windows
    sed -i 's/\r$//' brn_network.env 2>/dev/null || true
    set -a
    # shellcheck disable=SC1091
    source brn_network.env
    set +a
    ok "brn_network.env carregado"
else
    warn "brn_network.env não existe"
    info "Criando um brn_network.env básico com defaults..."
    cat > brn_network.env << 'ENVEOF'
# ============================================================
# brn_network.env — criado por iniciar.sh
# Edite os valores abaixo antes de usar em produção.
# ============================================================

# Segredos compartilhados (IGUAIS em todas as máquinas)
BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
BRN_NODE_PASSWORD=senha-no-temp-123
BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
BRN_WEB_PASS=senha-temp-123

# Admin token (protege /api/faucet)
BRN_ADMIN_TOKEN=

# Bootstrap
BRN_BOOTSTRAP_PEERS=

# Modo (1 = minera, 0 = cliente)
BRN_IS_ORIGIN=0

# Dificuldade
BRN_MIN_DIFFICULTY=6

# Portas
BRN_P2P_PORT=6001
BRN_WEB_PORT=5000
BRN_EXPLORER_PORT=8080

# MongoDB (opcional)
MONGO_URI=
MONGO_USER=
MONGO_PASS=
MONGO_HOST=
MONGO_DB=brn_analytics

# Logs
BRN_LOG_LEVEL=INFO
ENVEOF
    chmod 600 brn_network.env
    ok "brn_network.env criado (edite antes de usar)"
    # Recarrega
    set -a
    # shellcheck disable=SC1091
    source brn_network.env
    set +a
fi
echo ""

# ------------------------------------------------------------
# 2) Determinar modo (origin/client)
# ------------------------------------------------------------
if [ -z "$MODE" ]; then
    if [ "${BRN_IS_ORIGIN:-0}" = "1" ]; then
        MODE="origin"
    else
        MODE="client"
    fi
fi

if [ "$MODE" = "origin" ]; then
    export BRN_IS_ORIGIN=1
    export BRN_CLIENT_MODE=0
    echo -e "${C_CYAN}Modo:${C_RESET} ${C_BOLD}HUB / ORIGEM${C_RESET}"
else
    export BRN_IS_ORIGIN=0
    export BRN_CLIENT_MODE=1
    echo -e "${C_CYAN}Modo:${C_RESET} ${C_BOLD}CLIENTE${C_RESET}"
fi

export PYTHONUNBUFFERED=1
echo ""

# ------------------------------------------------------------
# 3) Auto-config de peers via Tailscale (opcional)
# ------------------------------------------------------------
if [ "$SKIP_AUTOCONFIG" = "0" ] && [ -f "brn_autoconfig.py" ]; then
    echo -e "${C_BOLD}[i] Auto-configurando peers via Tailscale...${C_RESET}"
    echo ""
    if "$PYTHON_BIN" brn_autoconfig.py --check 2>&1; then
        echo ""
        ok "Auto-config concluído"
    else
        echo ""
        warn "Auto-config retornou código não-zero — continuando"
    fi
    echo ""
elif [ "$SKIP_AUTOCONFIG" = "1" ]; then
    info "Auto-config pulado por --no-autoconfig"
    echo ""
else
    info "brn_autoconfig.py não encontrado — pulando"
    echo ""
fi

# ------------------------------------------------------------
# 4) Validar arquivos essenciais
# ------------------------------------------------------------
echo -e "${C_BOLD}[i] Validando arquivos...${C_RESET}"
REQUIRED=(
    "main.py"
    "server.py"
    "blockchain.py"
    "wallet.py"
    "db.py"
    "p2p_unified.py"
    "p2p_auth.py"
    "p2p_cipher.py"
    "p2p_secure.py"
    "p2p_relay.py"
    "security.py"
    "secure_store.py"
    "mongo_client.py"
    "brn_config.py"
    "brn_logger.py"
    "checkpoints.py"
    "chain_validator.py"
    "explorer.py"
    "miner_loop.py"
    "sync_manager.py"
    "discovery_v2.py"
    "rendezvous_discovery.py"
    "rendezvous_mongo.py"
    "l2_manager.py"
    "btc_watcher.py"
    "btc_config.py"
    "contracts/__init__.py"
    "contracts/vm.py"
    "contracts/escrow_btc_brn.py"
    "contracts/factory.py"
)
FALTANDO=()
for f in "${REQUIRED[@]}"; do
    if [ ! -f "$f" ]; then
        echo -e "   ${C_RED}[X] Faltando:${C_RESET} $f"
        FALTANDO+=("$f")
    else
        echo -e "   ${C_GREEN}[ok]${C_RESET} $f"
    fi
done
if [ ${#FALTANDO[@]} -gt 0 ]; then
    echo ""
    die "Arquivos faltando: ${FALTANDO[*]}"
fi
echo ""

# ------------------------------------------------------------
# 5) Verificar pymongo
# ------------------------------------------------------------
if ! "$PYTHON_BIN" -c "import pymongo" >/dev/null 2>&1; then
    warn "pymongo não instalado (Mongo desabilitado)"
    echo "    Para instalar: $PYTHON_BIN -m pip install 'pymongo[srv]' --user --break-system-packages"
else
    ok "pymongo já instalado"
fi
echo ""

# ------------------------------------------------------------
# 6) Testar Mongo (se configurado)
# ------------------------------------------------------------
MONGO_CONFIGURED=0
if [ -n "${MONGO_URI:-}" ]; then
    MONGO_CONFIGURED=1
elif [ -n "${MONGO_USER:-}" ] && [ -n "${MONGO_PASS:-}" ] && [ -n "${MONGO_HOST:-}" ]; then
    MONGO_CONFIGURED=1
fi

if [ "$SKIP_MONGO" = "1" ]; then
    info "Mongo: teste pulado (--no-mongo)"
elif [ "$MONGO_CONFIGURED" = "1" ]; then
    echo -e "${C_BOLD}[i] Testando conexão MongoDB...${C_RESET}"
    "$PYTHON_BIN" -c "
try:
    from mongo_client import mongo
    print('  configurado:', mongo.is_configured())
    if mongo.uri:
        print('  uri:', mongo.uri[:40] + '...')
    ok, err = mongo.ping()
    print('  ping:', 'OK' if ok else f'FALHOU — {err}')
except Exception as e:
    print(f'  erro: {e}')
" 2>&1 || warn "Falha no teste do Mongo"
    echo ""
else
    info "Mongo: não configurado (MONGO_URI/MONGO_USER vazios)"
    echo ""
fi

# ------------------------------------------------------------
# 7) Reset de identidade (opcional)
# ------------------------------------------------------------
if [ "$RESET_IDENTITY" = "1" ]; then
    info "Apagando identidade (--reset-identity)..."
    rm -f node_identity.enc node_identity.enc.bak node_identity.enc.oldformat.bak
    ok "Identidade removida"
    echo ""
fi

# ------------------------------------------------------------
# 8) Detectar formato antigo de node_identity.enc
# ------------------------------------------------------------
if [ -f "node_identity.enc" ]; then
    if ! "$PYTHON_BIN" -c "
import sys
f = open('node_identity.enc','rb'); f.seek(4); b = f.read(1); f.close()
sys.exit(0 if b == b'\x01' else 1)
" >/dev/null 2>&1; then
        warn "node_identity.enc em formato antigo - fazendo backup"
        mv -f node_identity.enc node_identity.enc.oldformat.bak
        ok "Backup salvo em node_identity.enc.oldformat.bak"
        echo ""
    else
        info "node_identity.enc em formato atual - mantendo"
        echo ""
    fi
else
    info "node_identity.enc será criado no primeiro boot"
    echo ""
fi

# ------------------------------------------------------------
# 9) Atualizar bootstrap_peers.json (do env, se definido)
# ------------------------------------------------------------
if [ -n "${BRN_BOOTSTRAP_PEERS:-}" ]; then
    PEERS_JSON="["
    FIRST=1
    IFS=',' read -ra PEERS <<< "$BRN_BOOTSTRAP_PEERS"
    for p in "${PEERS[@]}"; do
        p="$(echo "$p" | xargs)"
        [ -z "$p" ] && continue
        if [ "$FIRST" = "1" ]; then
            PEERS_JSON="${PEERS_JSON}\"${p}\""
            FIRST=0
        else
            PEERS_JSON="${PEERS_JSON},\"${p}\""
        fi
    done
    PEERS_JSON="${PEERS_JSON}]"
    echo "$PEERS_JSON" > bootstrap_peers.json
    ok "bootstrap_peers.json = $(cat bootstrap_peers.json)"
    echo ""
else
    if [ -f "bootstrap_peers.json" ]; then
        info "bootstrap_peers.json mantido: $(cat bootstrap_peers.json)"
    else
        echo "[]" > bootstrap_peers.json
        info "bootstrap_peers.json vazio"
    fi
    echo ""
fi

# ------------------------------------------------------------
# 10) Migrar current_wallet.json legado (se existir)
# ------------------------------------------------------------
if [ -f "current_wallet.json" ] && [ ! -f "current_wallet.enc" ]; then
    warn "Detectado current_wallet.json em TEXTO PURO"
    info "Migrando para formato cifrado..."
    "$PYTHON_BIN" -c "
try:
    from wallet import WalletManager
    import os
    p = os.environ.get('BRN_WALLET_SESSION_PASS', '')
    print(WalletManager.migrate_legacy_current(p))
except Exception as e:
    print(f'migracao falhou: {e}')
" 2>&1 || warn "Migração falhou (não crítico)"
    echo ""
fi

# ------------------------------------------------------------
# 11) Testar conexão com peer (só no modo cliente)
# ------------------------------------------------------------
if [ "$MODE" = "client" ] && [ -n "${BRN_BOOTSTRAP_PEERS:-}" ]; then
    PEER="${BRN_BOOTSTRAP_PEERS%%,*}"
    HOST="${PEER%%:*}"
    PORT="${PEER##*:}"
    echo -e "${C_BOLD}[i] Testando conexão com $HOST:$PORT...${C_RESET}"

    if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$HOST/$PORT" 2>/dev/null; then
        ok "Peer acessível em $HOST:$PORT"
    else
        warn "Não alcancei $HOST:$PORT"
        echo "    Verifique:"
        echo "      - A outra máquina está rodando?"
        echo "      - O IP $HOST está correto?"
        echo "      - Firewall liberou porta $PORT?"
        echo "    Continuando mesmo assim..."
    fi
    echo ""
fi

# ------------------------------------------------------------
# 12) RESUMO DA CONFIG
# ------------------------------------------------------------
echo -e "${C_BOLD}============================================================${C_RESET}"
echo -e "${C_BOLD}  Configuração final${C_RESET}"
echo -e "${C_BOLD}============================================================${C_RESET}"
echo -e "  Modo          : ${C_BOLD}${MODE}${C_RESET}"
echo -e "  IS_ORIGIN     : ${BRN_IS_ORIGIN:-0}"
echo -e "  Bootstrap     : ${BRN_BOOTSTRAP_PEERS:-(vazio)}"
echo -e "  P2P porta     : ${BRN_P2P_PORT:-6001}"
echo -e "  HTTP porta    : ${BRN_WEB_PORT:-5000}"
echo -e "  Explorer porta: ${BRN_EXPLORER_PORT:-8080}"
echo -e "  Dificuldade   : ${BRN_MIN_DIFFICULTY:-5}"
echo -e "  Mongo         : $([ "$MONGO_CONFIGURED" = "1" ] && echo 'configurado' || echo 'não configurado')"
echo -e "  Admin token   : $([ -n "${BRN_ADMIN_TOKEN:-}" ] && echo 'definido' || echo 'NÃO DEFINIDO')"
echo -e "${C_BOLD}============================================================${C_RESET}"
echo ""

# ------------------------------------------------------------
# 13) Iniciar o nó
# ------------------------------------------------------------
echo -e "${C_BOLD}Iniciando BRN Node (modo ${MODE^^})...${C_RESET}"
echo -e "  Ctrl+C para encerrar"
echo ""

MAIN_ARGS=()
if [ "$MODE" = "client" ]; then
    MAIN_ARGS+=("--client-mode")
fi
if [ "$HEADLESS" = "1" ]; then
    MAIN_ARGS+=("--headless")
fi
if [ "$SKIP_VERIFY" = "1" ]; then
    MAIN_ARGS+=("--skip-verify")
fi

"$PYTHON_BIN" main.py "${MAIN_ARGS[@]}" 2>&1 | tee run.log
EXITCODE=${PIPESTATUS[0]}

echo ""
echo -e "${C_BOLD}============================================================${C_RESET}"
if [ "$EXITCODE" -ne 0 ]; then
    err "Nó saiu com código $EXITCODE"
    echo ""
    echo -e "${C_YELLOW}Dicas:${C_RESET}"
    echo "  - \"falha ao decifrar\"      → ./iniciar.sh --reset-identity"
    echo "  - \"Aguardando genesis\"     → outra máquina não está rodando"
    echo "  - \"ModuleNotFoundError\"    → pip install -r requirements.txt"
    echo ""
    echo -e "${C_BOLD}Últimas 20 linhas do log:${C_RESET}"
    echo "------------------------------------------------------------"
    tail -n 20 run.log 2>/dev/null || echo "(sem log)"
    echo "------------------------------------------------------------"
else
    ok "Nó encerrado normalmente."
    echo ""
    tail -n 5 run.log 2>/dev/null || true
fi
echo -e "${C_BOLD}============================================================${C_RESET}"
echo ""

exit "$EXITCODE"