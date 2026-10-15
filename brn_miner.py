"""brn_miner.py — Wrapper ctypes para o PoW em C (v3 MT)
================================================================
Carrega libbrn_miner.so (Linux) ou brn_miner.dll (Windows).

Por que "lib" no nome?
    Se o arquivo for só "brn_miner.so", o Python confunde com
    uma extensão Python e tenta importá-lo como módulo — o que
    quebra com "PyInit_brn_miner not defined".
    Usar "libbrn_miner.so" (padrão Linux) evita isso.

API:
    is_available()                       -> bool
    has_mt()                             -> bool
    n_cores()                            -> int
    pow_search(...)                      -> single-thread
    pow_search_mt(..., n_threads=None)   -> multi-thread (auto: cores)
    benchmark(iters)                     -> single h/s
    benchmark_mt(iters, n_threads)       -> multi h/s
    info()                               -> dict
================================================================
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path


_LIB = None
_LIB_PATH = None
_LOAD_ATTEMPTED = False


def _library_candidates():
    """Lista de caminhos onde procurar a biblioteca (em ordem)."""
    base = Path(__file__).resolve().parent
    sysname = platform.system().lower()

    if sysname == "linux":
        names = ["libbrn_miner.so", "brn_miner.so"]
    elif sysname == "darwin":
        names = ["libbrn_miner.dylib", "libbrn_miner.so",
                 "brn_miner.dylib", "brn_miner.so"]
    elif sysname == "windows":
        names = ["brn_miner.dll", "libbrn_miner.dll"]
    else:
        names = ["libbrn_miner.so", "brn_miner.so"]

    candidates = [base / n for n in names]
    candidates.append(Path("/usr/local/lib") / names[0])
    candidates.append(Path("/usr/lib") / names[0])
    return candidates


def _load():
    """Carrega a lib uma única vez. Retorna a lib ou None."""
    global _LIB, _LIB_PATH, _LOAD_ATTEMPTED

    if _LIB is not None:
        return _LIB
    if _LOAD_ATTEMPTED:
        return None
    _LOAD_ATTEMPTED = True

    for path in _library_candidates():
        if not path.exists():
            continue
        try:
            lib = ctypes.CDLL(str(path))
        except Exception as e:
            print(f"[brn_miner] falha carregando {path}: {e}",
                  file=sys.stderr)
            continue

        # --- brn_pow_search (single-thread) ---
        lib.brn_pow_search.restype = ctypes.c_int
        lib.brn_pow_search.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]

        # --- brn_benchmark (single) ---
        lib.brn_benchmark.restype = ctypes.c_double
        lib.brn_benchmark.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_int, ctypes.c_uint64,
        ]

        # --- brn_pow_search_mt (multi-thread, opcional) ---
        try:
            lib.brn_pow_search_mt.restype = ctypes.c_int
            lib.brn_pow_search_mt.argtypes = [
                ctypes.c_char_p, ctypes.c_size_t,
                ctypes.c_char_p, ctypes.c_size_t,
                ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
            ]
        except AttributeError:
            pass

        try:
            lib.brn_benchmark_mt.restype = ctypes.c_double
            lib.brn_benchmark_mt.argtypes = [
                ctypes.c_char_p, ctypes.c_size_t,
                ctypes.c_char_p, ctypes.c_size_t,
                ctypes.c_int, ctypes.c_uint64, ctypes.c_int,
            ]
        except AttributeError:
            pass

        try:
            lib.brn_n_cores.restype = ctypes.c_int
            lib.brn_n_cores.argtypes = []
        except AttributeError:
            pass

        _LIB = lib
        _LIB_PATH = path
        print(f"[brn_miner] carregada: {path}")
        return lib

    return None


# ============================================================
# API BÁSICA
# ============================================================
def is_available() -> bool:
    return _load() is not None


def has_mt() -> bool:
    lib = _load()
    return lib is not None and hasattr(lib, "brn_pow_search_mt")


def n_cores() -> int:
    lib = _load()
    if lib is not None and hasattr(lib, "brn_n_cores"):
        try:
            return int(lib.brn_n_cores())
        except Exception:
            pass
    return os.cpu_count() or 1


def library_path():
    _load()
    return _LIB_PATH


# ============================================================
# SINGLE-THREAD
# ============================================================
def pow_search(prefix: str, diff_str: str, difficulty: int,
               nonce_start: int = 0, max_iters: int = 0):
    """
    Retorna:
        (nonce, hash_hex, hashes) — encontrou
        (None,  None,     hashes) — esgotou max_iters
        None                      — biblioteca indisponível
    """
    lib = _load()
    if lib is None:
        return None

    pfx = prefix.encode() if isinstance(prefix, str) else prefix
    dif = diff_str.encode() if isinstance(diff_str, str) else diff_str

    out_hex = ctypes.create_string_buffer(65)
    out_nonce = ctypes.c_uint64(0)
    out_hashes = ctypes.c_uint64(0)

    rc = lib.brn_pow_search(
        pfx, len(pfx), dif, len(dif),
        ctypes.c_uint64(nonce_start), ctypes.c_uint64(max_iters),
        ctypes.c_int(difficulty),
        out_hex,
        ctypes.byref(out_nonce),
        ctypes.byref(out_hashes),
    )

    if rc == 0:
        return (out_nonce.value, out_hex.value.decode("ascii"),
                out_hashes.value)
    if rc == 1:
        return (None, None, out_hashes.value)
    return None


# ============================================================
# MULTI-THREAD
# ============================================================
def pow_search_mt(prefix: str, diff_str: str, difficulty: int,
                  nonce_start: int = 0, max_iters: int = 0,
                  n_threads: int = None):
    lib = _load()
    if lib is None:
        return None

    if not hasattr(lib, "brn_pow_search_mt"):
        # Lib antiga sem MT — delega para single
        return pow_search(prefix, diff_str, difficulty,
                          nonce_start, max_iters)

    if n_threads is None:
        n_threads = n_cores()
    if n_threads < 1:
        n_threads = 1

    pfx = prefix.encode() if isinstance(prefix, str) else prefix
    dif = diff_str.encode() if isinstance(diff_str, str) else diff_str

    out_hex = ctypes.create_string_buffer(65)
    out_nonce = ctypes.c_uint64(0)
    out_hashes = ctypes.c_uint64(0)

    rc = lib.brn_pow_search_mt(
        pfx, len(pfx), dif, len(dif),
        ctypes.c_uint64(nonce_start), ctypes.c_uint64(max_iters),
        ctypes.c_int(difficulty),
        ctypes.c_int(n_threads),
        out_hex,
        ctypes.byref(out_nonce),
        ctypes.byref(out_hashes),
    )

    if rc == 0:
        return (out_nonce.value, out_hex.value.decode("ascii"),
                out_hashes.value)
    if rc == 1:
        return (None, None, out_hashes.value)
    return None


# ============================================================
# BENCHMARKS
# ============================================================
def benchmark(iters: int = 100_000) -> float:
    lib = _load()
    if lib is None:
        return 0.0
    pfx = b"benchmark_prefix_hash_merkle_timestamp"
    dif = b"5"
    return float(lib.brn_benchmark(pfx, len(pfx), dif, len(dif), 5, iters))


def benchmark_mt(iters: int = 500_000, n_threads: int = None) -> float:
    lib = _load()
    if lib is None:
        return 0.0
    if not hasattr(lib, "brn_benchmark_mt"):
        return 0.0
    if n_threads is None:
        n_threads = n_cores()
    pfx = b"benchmark_prefix_hash_merkle_timestamp"
    dif = b"5"
    return float(lib.brn_benchmark_mt(pfx, len(pfx), dif, len(dif), 5,
                                       iters, n_threads))


# ============================================================
# INFO
# ============================================================
def info() -> dict:
    lib = _load()
    return {
        "available":   lib is not None,
        "has_mt":      has_mt(),
        "path":        str(_LIB_PATH) if _LIB_PATH else None,
        "platform":    platform.system(),
        "arch":        platform.machine(),
        "n_cores":     n_cores(),
        "cpu_count":   os.cpu_count() or 1,
    }


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print(" brn_miner.py — status (v3 MT)")
    print("=" * 60)
    for k, v in info().items():
        print(f"  {k}: {v}")

    if not is_available():
        print()
        print("[X] Biblioteca nao encontrada.")
        print("    Compile com:")
        print("      gcc -O3 -march=native -funroll-loops -pthread \\")
        print("          -shared -fPIC -o libbrn_miner.so brn_miner.c -lcrypto")
        print()
        print("    Ou renomeie:")
        print("      mv brn_miner.so libbrn_miner.so")
        raise SystemExit(1)

    print()
    print("Benchmark single-thread (200k hashes)...")
    hr1 = benchmark(200_000)
    print(f"  C single: {hr1:>12,.0f} hashes/s")

    import hashlib, time
    prefix = "benchmark_prefix_hash_merkle_timestamp"
    diff_str = "5"
    N = 200_000
    t0 = time.time()
    sha = hashlib.sha256
    for i in range(N):
        data = (prefix + str(i) + diff_str).encode()
        sha(sha(data).digest()).digest()
    hr_py = N / (time.time() - t0)
    print(f"  Python  : {hr_py:>12,.0f} hashes/s")
    print(f"  Speedup : {hr1/hr_py:.2f}x")

    if has_mt():
        cores = n_cores()
        print()
        print(f"Benchmark multi-thread ({cores} cores, 1M hashes)...")
        hr_mt = benchmark_mt(1_000_000, n_threads=cores)
        print(f"  C MT x{cores}: {hr_mt:>11,.0f} hashes/s")
        print(f"  Speedup vs single C : {hr_mt/hr1:.2f}x")
        print(f"  Speedup vs Python   : {hr_mt/hr_py:.2f}x")