#!/usr/bin/env python3
"""Teste de velocidade real de mineração."""
import os
import time
import tempfile

os.environ.setdefault("BRN_NETWORK_SECRET", "teste-speed-1234567890")

from blockchain import Blockchain
from brn_miner import is_available, has_mt, n_cores


def main():
    print("=" * 60)
    print(" BRN — Teste de velocidade real")
    print("=" * 60)

    print(f"\nC disponivel:  {is_available()}")
    print(f"Multi-thread:  {has_mt()}")
    print(f"Cores:         {n_cores()}")
    print()

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "speed.db")
        bc = Blockchain(db, auto_genesis=True)

        addr = "brn1qxyzk7y0v2j4g0a8d9n5t2k7h4s6w8c9p2e"
        pub = "02" + "aa" * 32

        print("Minerando 5 blocos:")
        times = []
        for i in range(5):
            t0 = time.time()
            blk = bc.mine_block(addr, pub)
            dt = time.time() - t0
            times.append(dt)
            print(f"  #{blk['height']}  {dt:.3f}s  diff={blk['difficulty']}")

        media = sum(times) / len(times)
        print()
        print(f"Tempo medio: {media:.3f}s por bloco")
        print(f"Blocos/min:  {60/media:.1f}")
        print(f"BRN/hora:    {(3600/media)*50:.0f}")


if __name__ == "__main__":
    main()
