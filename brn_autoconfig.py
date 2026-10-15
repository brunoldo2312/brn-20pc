#!/usr/bin/env python3
"""brn_autoconfig.py — Auto-configuração do nó BRN
================================================================
Detecta peers via Tailscale, corrige bootstrap, testa conectividade
e mostra relatório do estado do nó.

Uso:
    python3 brn_autoconfig.py            # interativo
    python3 brn_autoconfig.py --check    # só mostra, não altera
    python3 brn_autoconfig.py --auto     # aplica sem perguntar

Funciona em Linux e Windows (apenas stdlib).
================================================================
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


# ============================================================
# CONSTANTES
# ============================================================
BASE = Path(__file__).resolve().parent
ENV_FILE       = BASE / "brn_network.env"
BOOTSTRAP_FILE = BASE / "bootstrap_peers.json"
TAILSCALE_PEER = BASE / "brn_tailscale_peer.txt"

API_BASE = "http://127.0.0.1:5000"

IS_WINDOWS = platform.system().lower().startswith("win")
IS_LINUX   = platform.system().lower() == "linux"


# ============================================================
# CORES
# ============================================================
class C:
    OK   = "\033[92m"
    WARN = "\033[93m"
    ERR  = "\033[91m"
    INFO = "\033[96m"
    BOLD = "\033[1m"
    RST  = "\033[0m"


def ok(m):    print(f"{C.OK}[ok]{C.RST} {m}")
def warn(m):  print(f"{C.WARN}[!]{C.RST} {m}")
def err(m):   print(f"{C.ERR}[X]{C.RST} {m}")
def info(m):  print(f"{C.INFO}[i]{C.RST} {m}")
def title(m): print(f"\n{C.BOLD}═══ {m} ═══{C.RST}")


# ============================================================
# HELPERS DE SISTEMA
# ============================================================
def run_cmd(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, shell=False)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "cmd not found"
    except subprocess.TimeoutExpired:
        return -2, "", "timeout"
    except Exception as e:
        return -3, "", str(e)


def find_tailscale():
    for name in ("tailscale", "tailscale.exe"):
        rc, _, _ = run_cmd([name, "version"])
        if rc == 0:
            return name
    if IS_WINDOWS:
        for p in (r"C:\Program Files\Tailscale\tailscale.exe",
                  r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
            if Path(p).exists():
                return p
    if IS_LINUX:
        for p in ("/usr/bin/tailscale", "/usr/local/bin/tailscale"):
            if Path(p).exists():
                return p
    return None


def http_get_json(url, timeout=5.0):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "brn-autoconfig"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return False, str(e)


def tcp_check(host, port, timeout=3.0):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


# ============================================================
# LEITURA / ESCRITA
# ============================================================
def load_env_file():
    out = {}
    if not ENV_FILE.exists():
        return out
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                out[k] = v
    except Exception:
        pass
    return out


def save_env_file(data):
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        if s.startswith("#") or not s:
            out.append(line)
            continue
        if "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in data:
                out.append(f"{k}={data[k]}")
                seen.add(k)
            else:
                out.append(line)
        else:
            out.append(line)
    for k, v in data.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def save_bootstrap(peers):
    BOOTSTRAP_FILE.write_text(
        json.dumps(peers, indent=2, ensure_ascii=False),
        encoding="utf-8")


# ============================================================
# DETECÇÃO DE REDE
# ============================================================
def detect_tailscale_ips():
    out = {"self": "", "peers": [], "names": {}, "available": False}
    ts = find_tailscale()
    if not ts:
        return out
    rc, stdout, _ = run_cmd([ts, "status", "--json"], timeout=5)
    if rc != 0:
        rc, stdout, _ = run_cmd([ts, "status"], timeout=5)
        if rc != 0:
            return out
    out["available"] = True
    if stdout.startswith("{"):
        try:
            data = json.loads(stdout)
            self_info = data.get("Self") or {}
            out["self"] = (self_info.get("TailscaleIPs") or [""])[0]
            for _, peer in (data.get("Peer") or {}).items():
                if peer.get("Online", False) is False:
                    continue
                ips = peer.get("TailscaleIPs") or []
                if not ips:
                    continue
                ip = ips[0]
                if ip and ip != out["self"]:
                    out["peers"].append(ip)
                    out["names"][ip] = peer.get("HostName", ip)
            return out
        except Exception:
            pass
    for line in stdout.splitlines():
        m = re.match(r"^(100\.\d+\.\d+\.\d+)\s+(\S+)\s+", line)
        if m:
            ip, name = m.group(1), m.group(2)
            if "self" in line.lower() or ip == out["self"]:
                out["self"] = ip
                continue
            if ip not in out["peers"]:
                out["peers"].append(ip)
                out["names"][ip] = name
    if not out["self"]:
        rc, ip_stdout, _ = run_cmd([ts, "ip", "-4"], timeout=5)
        if rc == 0:
            for line in ip_stdout.splitlines():
                line = line.strip()
                if line.startswith("100."):
                    out["self"] = line
                    break
    return out


def detect_lan_ips():
    ips = set()
    for probe in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect((probe, 1))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def same_subnet(ip1, ip2):
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception:
        return False


def node_running():
    ok_flag, _ = http_get_json(f"{API_BASE}/api/status")
    return ok_flag


# ============================================================
# ATUALIZAÇÃO DE CONFIGS
# ============================================================
def update_configs(peer_ips, dry=False):
    result = {"env_changed": False, "bootstrap_changed": False}

    env = load_env_file()
    cur_str = env.get("BRN_BOOTSTRAP_PEERS", "")
    cur_peers = [p.strip() for p in cur_str.split(",") if p.strip()]

    p2p_port = env.get("BRN_P2P_PORT", "6001")
    new_peers = []
    for ip in peer_ips:
        entry = ip if ":" in ip else f"{ip}:{p2p_port}"
        if entry not in new_peers:
            new_peers.append(entry)
    for old in cur_peers:
        if old not in new_peers:
            new_peers.append(old)

    new_str = ",".join(new_peers)

    if new_str != cur_str:
        result["env_changed"] = True
        if not dry:
            env["BRN_BOOTSTRAP_PEERS"] = new_str
            save_env_file(env)

    cur_json = []
    if BOOTSTRAP_FILE.exists():
        try:
            cur_json = json.loads(
                BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            if not isinstance(cur_json, list):
                cur_json = []
        except Exception:
            cur_json = []

    merged = list(new_peers)
    for old in cur_json:
        if old not in merged:
            merged.append(old)

    if merged != cur_json:
        result["bootstrap_changed"] = True
        if not dry:
            save_bootstrap(merged)

    return result


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(prog="brn_autoconfig.py")
    parser.add_argument("--auto", action="store_true",
                        help="Aplica sem perguntar")
    parser.add_argument("--check", action="store_true",
                        help="Só mostra, não altera")
    args = parser.parse_args()

    print(f"\n{C.BOLD}{'=' * 64}{C.RST}")
    print(f"{C.BOLD}  BRN Auto-Config{C.RST}")
    print(f"{C.BOLD}  {socket.gethostname()} — {platform.system()}{C.RST}")
    print(f"{C.BOLD}{'=' * 64}{C.RST}")

    # 1) Tailscale
    title("1. Tailscale")
    ts = detect_tailscale_ips()
    if not ts["available"]:
        warn("Tailscale não encontrado ou não instalado")
    else:
        ok(f"Meu IP: {ts['self'] or '?'}")
        if ts["peers"]:
            for ip in ts["peers"]:
                info(f"  peer: {ip} ({ts['names'].get(ip, '?')})")
        else:
            warn("Nenhum peer Tailscale online")

    # 2) LAN
    title("2. IPs LAN")
    lan = detect_lan_ips()
    for ip in lan:
        info(f"  {ip}")

    # 3) Config atual
    title("3. Configuração atual")
    env = load_env_file()
    cur_str = env.get("BRN_BOOTSTRAP_PEERS", "")
    cur_peers = [p.strip() for p in cur_str.split(",") if p.strip()]
    info(f"BRN_BOOTSTRAP_PEERS = {cur_str or '(vazio)'}")

    cur_bootstrap = []
    if BOOTSTRAP_FILE.exists():
        try:
            cur_bootstrap = json.loads(
                BOOTSTRAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    info(f"bootstrap_peers.json = {cur_bootstrap or '(vazio)'}")

    # 4) Peers candidatos
    title("4. Peers candidatos")
    candidates = []
    for ip in ts.get("peers", []):
        if ip != ts.get("self") and ip not in candidates:
            candidates.append(ip)

    if not candidates and lan:
        primary = lan[0]
        for ip in lan[1:]:
            if same_subnet(primary, ip) and ip not in candidates:
                candidates.append(ip)

    if not candidates and TAILSCALE_PEER.exists():
        content = TAILSCALE_PEER.read_text(encoding="utf-8").strip()
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ip = line.split(":")[0]
                if ip not in candidates:
                    candidates.append(ip)

    if candidates:
        ok(f"Peers detectados: {candidates}")
    else:
        warn("Nenhum peer detectado automaticamente")

    # 5) Aplicar
    title("5. Aplicando configuração")
    if not candidates and not cur_peers:
        warn("Sem peers — pulando atualização")
        peers_after = cur_peers
    else:
        changes = update_configs(candidates, dry=args.check)
        if changes["env_changed"]:
            if args.check:
                info("(modo --check) brn_network.env SERIA alterado")
            else:
                ok("brn_network.env atualizado")
        else:
            info("brn_network.env já correto")

        if changes["bootstrap_changed"]:
            if args.check:
                info("(modo --check) bootstrap_peers.json SERIA alterado")
            else:
                ok("bootstrap_peers.json atualizado")
        else:
            info("bootstrap_peers.json já correto")

        env = load_env_file()
        peers_after = [p.strip() for p in
                       env.get("BRN_BOOTSTRAP_PEERS", "").split(",")
                       if p.strip()]

    # 6) Consultar nó
    title("6. Consultando o nó")
    node_ok = node_running()
    chain = {}
    sync = {}
    peers_db = {}

    if node_ok:
        ok("HTTP :5000 respondendo")
        co, c = http_get_json(f"{API_BASE}/api/chain-info")
        if co and isinstance(c, dict):
            chain = c
        so, s = http_get_json(f"{API_BASE}/api/sync/status")
        if so and isinstance(s, dict):
            sync = s
        po, p = http_get_json(f"{API_BASE}/api/peers/score")
        if po and isinstance(p, dict):
            peers_db = p
    else:
        err("HTTP :5000 NÃO respondeu")

    # 7) Relatório
    title("RELATÓRIO FINAL")
    info(f"Peers antes:  {cur_peers or '(vazio)'}")
    info(f"Peers depois: {peers_after or '(vazio)'}")

    if chain:
        info(f"Altura:    {chain.get('height')}")
        info(f"Tip hash:  {str(chain.get('tip_hash', ''))[:32]}...")
        info(f"Dificuldade: {chain.get('difficulty')}")

    if sync:
        info(f"Sync: {sync.get('progress')}% — "
             f"synced={sync.get('synced')}")
        info(f"Local:  {sync.get('local_height')} → "
             f"Target: {sync.get('target_height')}")

    if peers_db:
        peers_list = peers_db.get("peers", [])
        ok(f"Peers no DB: {len(peers_list)}")
        for p in peers_list:
            addr = p.get("address", "?")
            score = p.get("score", 0)
            last = p.get("last_seen", 0)
            age = int(time.time() - last) if last else 0
            info(f"  {addr:<24} score={score} last_seen={age}s atrás")

    # 8) Testes TCP
    title("Testes TCP")
    if peers_after:
        for p in peers_after:
            if ":" in p:
                ip, port = p.rsplit(":", 1)
                try:
                    port_i = int(port)
                except ValueError:
                    continue
                if tcp_check(ip, port_i):
                    ok(f"TCP {p} — alcançável")
                else:
                    warn(f"TCP {p} — inalcançável")
    else:
        info("(nenhum peer para testar)")

    # 9) Conclusão
    print()
    print(f"{C.BOLD}{'=' * 64}{C.RST}")
    if node_ok and sync.get("synced"):
        print(f"{C.OK}{C.BOLD}  ✓ Nó sincronizado e operacional{C.RST}")
    elif node_ok and chain:
        print(f"{C.WARN}{C.BOLD}  ⏳ Nó rodando, aguardando sync{C.RST}")
    elif node_ok:
        print(f"{C.WARN}{C.BOLD}  ⚠ Nó rodando mas chain vazia{C.RST}")
    else:
        print(f"{C.ERR}{C.BOLD}  ✗ Nó não está respondendo{C.RST}")
    print(f"{C.BOLD}{'=' * 64}{C.RST}\n")

    if not args.check and node_ok:
        print(f"{C.WARN}Se o bootstrap mudou, reinicie o nó:{C.RST}")
        print("  Ctrl+C na janela do nó e depois:")
        cmd = "python3" if IS_LINUX else "python"
        print(f"  {cmd} main.py --client-mode --no-tailscale --headless\n")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado pelo usuário.")
        sys.exit(130)
