"""
rendezvous_discovery.py — Fachada de descoberta remota (v2)
================================================================
Decide dinamicamente entre backends de rendezvous:

    Backend 1: RendezvousMongo (via MongoDB)
        - Requer mongo_client.py configurado
        - Ideal para peers em redes diferentes (NAT, Tailscale)

    Backend 2: No-op
        - Se nada estiver configurado, não faz nada (silencioso)

API compatível com main.py v12.6:

    rdv = RendezvousDiscovery(
        tcp_port=6001,
        node_uuid="<pubkey hex>",
        on_peer=lambda ip, port: p2p.add_peer(ip, port),
        running_flag=lambda: not _shutdown.is_set(),
        sign_fn=node_id_priv.sign,   # opcional (não usado no Mongo)
        blockchain=chain,            # v2: NOVO — usado pelo RendezvousMongo
    )
    rdv.start()
    ...
    rdv.stop()

v2:
  [NEW] __init__ aceita `blockchain=None` para compatibilidade com
        main.py v12.6. Antes, o main.py passava `blockchain=chain` e
        quebrava com "unexpected keyword argument 'blockchain'".
  [NEW] Repassa blockchain para RendezvousMongo.

Requer:
    rendezvous_mongo.py
    mongo_client.py (com is_configured, db)
================================================================
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional


log = logging.getLogger("rendezvous_discovery")


# ============================================================
# RendezvousDiscovery
# ============================================================
class RendezvousDiscovery:
    """
    Orquestra o backend de rendezvous. Atualmente suporta apenas MongoDB
    (via RendezvousMongo). Se o backend não estiver disponível, vira no-op.
    """

    def __init__(self,
                 tcp_port: int,
                 node_uuid: str,
                 on_peer: Callable[[str, int], None],
                 running_flag: Callable[[], bool],
                 sign_fn: Optional[Callable] = None,
                 blockchain=None) -> None:
        self.tcp_port = int(tcp_port)
        self.node_uuid = str(node_uuid or "")
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.sign_fn = sign_fn           # não usado por RendezvousMongo
        self.blockchain = blockchain     # v2: usado por RendezvousMongo

        self._impl = None
        self._running = False
        self._backend = "none"
        self._lock = threading.RLock()

        # Log config relacionada
        self._rdv_url = os.environ.get("BRN_RENDEZVOUS_URL", "").strip()

    # ============================================================
    # START
    # ============================================================
    def start(self) -> bool:
        """
        Tenta subir o backend de rendezvous.
        Retorna True se um backend ficou ativo.
        """
        with self._lock:
            if self._running:
                return True

            # ---- 1. MongoDB ----
            if self._try_mongo():
                self._running = True
                return True

            # ---- 2. HTTP Rendezvous (não implementado) ----
            if self._rdv_url:
                log.info("rendezvous: HTTP nao implementado — ignorando %s",
                         self._rdv_url)

            # ---- 3. No-op ----
            log.info("rendezvous: nenhum backend disponivel (no-op)")
            self._backend = "none"
            return False

    def _try_mongo(self) -> bool:
        """Tenta inicializar o backend MongoDB. Retorna True se OK."""
        try:
            from mongo_client import mongo
        except ImportError:
            log.debug("rendezvous: mongo_client nao encontrado")
            return False

        if not hasattr(mongo, "is_configured"):
            log.debug("rendezvous: mongo_client sem is_configured()")
            return False

        try:
            if not mongo.is_configured():
                log.debug("rendezvous: mongo nao configurado")
                return False
        except Exception as e:
            log.debug("rendezvous: is_configured falhou: %s", e)
            return False

        try:
            from rendezvous_mongo import RendezvousMongo
        except ImportError as e:
            log.warning("rendezvous: RendezvousMongo indisponivel: %s", e)
            return False

        try:
            self._impl = RendezvousMongo(
                tcp_port=self.tcp_port,
                node_uuid=self.node_uuid,
                on_peer=self.on_peer,
                running_flag=self.running_flag,
                blockchain=self.blockchain,   # v2
            )
            self._impl.start()
            self._backend = "mongo"
            log.info("rendezvous: MongoDB ativo")
            return True
        except Exception as e:
            log.warning("rendezvous: falha ao subir Mongo: %s", e)
            self._impl = None
            return False

    # ============================================================
    # STOP
    # ============================================================
    def stop(self) -> None:
        with self._lock:
            if self._impl is not None:
                try:
                    self._impl.stop()
                except Exception as e:
                    log.debug("rendezvous stop: %s", e)
                self._impl = None
            self._running = False

    # ============================================================
    # STATUS
    # ============================================================
    def get_status(self) -> dict:
        with self._lock:
            if self._impl is not None and hasattr(self._impl, "status"):
                try:
                    st = self._impl.status()
                    st["backend"] = self._backend
                    return st
                except Exception as e:
                    log.debug("rendezvous status: %s", e)
            return {
                "backend":  self._backend,
                "running":  self._running,
                "tcp_port": self.tcp_port,
                "node":     (self.node_uuid[:16] + "..."
                             if self.node_uuid else ""),
            }

    # ============================================================
    # INFO
    # ============================================================
    def is_active(self) -> bool:
        return self._running and self._impl is not None

    def backend_name(self) -> str:
        return self._backend


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando RendezvousDiscovery v2...")

    # ---- 1. Sem Mongo configurado: vira no-op ----
    received = []
    stop_flag = [False]

    rdv = RendezvousDiscovery(
        tcp_port=6001,
        node_uuid="abc" * 21 + "de",
        on_peer=lambda ip, port: received.append((ip, port)),
        running_flag=lambda: not stop_flag[0],
        blockchain=None,   # v2: aceita esse kwarg
    )

    ok = rdv.start()
    print(f"  start() retornou: {ok}")
    st = rdv.get_status()
    print(f"  status: {json.dumps(st, ensure_ascii=False)}")

    # ---- 2. Aceita todos os kwargs ----
    rdv2 = RendezvousDiscovery(
        tcp_port=6002,
        node_uuid="def" * 21 + "ab",
        on_peer=lambda ip, port: None,
        running_flag=lambda: False,
        sign_fn=lambda data: b"fake_sig",
        blockchain="fake_chain",
    )
    print(f"  [ok] aceita sign_fn + blockchain: backend={rdv2.backend_name()}")

    # ---- 3. Stop ----
    rdv.stop()
    print(f"  [ok] stop() — is_active={rdv.is_active()}")

    print("RendezvousDiscovery v2 OK.")