/*
 * brn_miner.c — PoW miner em C para BRN (v3 multi-thread)
 * ================================================================
 * Compilar:
 *   gcc -O3 -march=native -funroll-loops -pthread \
 *       -shared -fPIC -o brn_miner.so brn_miner.c -lcrypto
 *
 * Exporta:
 *   brn_pow_search()      — versão single-thread (compat)
 *   brn_pow_search_mt()   — versão multi-thread (nova)
 *   brn_benchmark()       — benchmark single-thread
 *   brn_benchmark_mt()    — benchmark multi-thread
 *   brn_n_cores()         — detecta cores disponíveis
 *
 * Estratégia MT:
 *   Thread i busca nonces { start+i, start+i+N, start+i+2N, ... }
 *   Primeira thread que acha sinaliza `found` e as outras param.
 *   A solução encontrada é válida (hash <= target), mesmo que não
 *   seja o menor nonce possível — o consenso aceita qualquer um.
 * ================================================================
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <stdatomic.h>
#include <openssl/sha.h>


/* ============================================================
 * CONVERSÃO RÁPIDA uint64 → DECIMAL
 * ============================================================ */
static inline int u64_to_dec(uint64_t v, char* out) {
    if (v < 10) {
        out[0] = (char)('0' + v);
        return 1;
    }
    char tmp[20];
    int n = 0;
    while (v >= 10) {
        tmp[n++] = (char)('0' + (v % 10));
        v /= 10;
    }
    tmp[n++] = (char)('0' + v);
    for (int i = 0; i < n; i++) out[i] = tmp[n - 1 - i];
    return n;
}


/* ============================================================
 * TARGET
 * ============================================================ */
static inline void make_target(int difficulty, unsigned char target[32]) {
    memset(target, 0xff, 32);
    int full_bytes   = difficulty / 2;
    int extra_nibble = difficulty % 2;
    for (int i = 0; i < full_bytes; i++) target[i] = 0x00;
    if (extra_nibble) target[full_bytes] = 0x0f;
}


/* ============================================================
 * SINGLE-THREAD (compat)
 * ============================================================ */
int brn_pow_search(
    const char* prefix, size_t prefix_len,
    const char* diff_str, size_t diff_str_len,
    uint64_t nonce_start,
    uint64_t max_iters,
    int difficulty,
    char* out_hash_hex,
    uint64_t* out_nonce,
    uint64_t* out_hashes
) {
    if (!prefix || !diff_str || !out_hash_hex || !out_nonce || !out_hashes)
        return -1;
    if (difficulty < 1 || difficulty > 62)
        return -1;

    unsigned char target[32];
    make_target(difficulty, target);

    size_t buf_size = prefix_len + 24 + diff_str_len + 8;
    char* data = (char*)malloc(buf_size);
    if (!data) return -1;
    memcpy(data, prefix, prefix_len);

    unsigned char hash1[SHA256_DIGEST_LENGTH];
    unsigned char hash2[SHA256_DIGEST_LENGTH];

    uint64_t nonce  = nonce_start;
    uint64_t hashes = 0;

    while (max_iters == 0 || hashes < max_iters) {
        int n = u64_to_dec(nonce, data + prefix_len);
        memcpy(data + prefix_len + n, diff_str, diff_str_len);
        size_t total = prefix_len + n + diff_str_len;

        SHA256((const unsigned char*)data, total, hash1);
        SHA256(hash1, SHA256_DIGEST_LENGTH, hash2);
        hashes++;

        if (memcmp(hash2, target, 32) <= 0) {
            static const char* hx = "0123456789abcdef";
            for (int i = 0; i < 32; i++) {
                out_hash_hex[i*2]   = hx[(hash2[i] >> 4) & 0xf];
                out_hash_hex[i*2+1] = hx[ hash2[i]       & 0xf];
            }
            out_hash_hex[64] = '\0';
            *out_nonce  = nonce;
            *out_hashes = hashes;
            free(data);
            return 0;
        }
        nonce++;
    }

    *out_nonce  = nonce;
    *out_hashes = hashes;
    free(data);
    return 1;
}


/* ============================================================
 * MULTI-THREAD
 * ============================================================ */
typedef struct {
    const char* prefix;
    size_t prefix_len;
    const char* diff_str;
    size_t diff_str_len;
    unsigned char target[32];
    int thread_id;
    int n_threads;
    uint64_t nonce_start;
    uint64_t max_hashes_per_thread;

    atomic_int* found;                 /* 0 = continua, 1 = achou */
    pthread_mutex_t* result_lock;
    uint64_t* result_nonce;
    char* result_hash;
    int* result_thread_id;

    uint64_t hashes_done;              /* só essa thread escreve */
} mt_worker_t;


static void* mt_worker(void* arg) {
    mt_worker_t* w = (mt_worker_t*)arg;

    size_t buf_size = w->prefix_len + 24 + w->diff_str_len + 8;
    char* data = (char*)malloc(buf_size);
    if (!data) { w->hashes_done = 0; return NULL; }
    memcpy(data, w->prefix, w->prefix_len);

    unsigned char hash1[SHA256_DIGEST_LENGTH];
    unsigned char hash2[SHA256_DIGEST_LENGTH];

    /* Esta thread busca:
     *   nonce_start + thread_id, nonce_start + thread_id + n_threads, ...
     */
    uint64_t nonce  = w->nonce_start + w->thread_id;
    uint64_t step   = (uint64_t)w->n_threads;
    uint64_t hashes = 0;
    uint64_t limit  = w->max_hashes_per_thread;

    const int CHECK_EVERY = 1024;

    while (limit == 0 || hashes < limit) {
        for (int j = 0; j < CHECK_EVERY && (limit == 0 || hashes < limit); j++) {
            int n = u64_to_dec(nonce, data + w->prefix_len);
            memcpy(data + w->prefix_len + n, w->diff_str, w->diff_str_len);
            size_t total = w->prefix_len + n + w->diff_str_len;

            SHA256((const unsigned char*)data, total, hash1);
            SHA256(hash1, SHA256_DIGEST_LENGTH, hash2);
            hashes++;

            if (memcmp(hash2, w->target, 32) <= 0) {
                /* ACHOU — registra resultado */
                pthread_mutex_lock(w->result_lock);
                if (!atomic_load(w->found)) {
                    static const char* hx = "0123456789abcdef";
                    for (int i = 0; i < 32; i++) {
                        w->result_hash[i*2]   = hx[(hash2[i] >> 4) & 0xf];
                        w->result_hash[i*2+1] = hx[ hash2[i]       & 0xf];
                    }
                    w->result_hash[64] = '\0';
                    *w->result_nonce = nonce;
                    *w->result_thread_id = w->thread_id;
                    atomic_store(w->found, 1);
                }
                pthread_mutex_unlock(w->result_lock);
                w->hashes_done = hashes;
                free(data);
                return NULL;
            }

            nonce += step;
        }

        /* Checa sinal global a cada chunk */
        if (atomic_load(w->found)) {
            w->hashes_done = hashes;
            free(data);
            return NULL;
        }
    }

    w->hashes_done = hashes;
    free(data);
    return NULL;
}


/* ============================================================
 * API MULTI-THREAD
 * ============================================================ */
int brn_pow_search_mt(
    const char* prefix, size_t prefix_len,
    const char* diff_str, size_t diff_str_len,
    uint64_t nonce_start,
    uint64_t max_iters,          /* total de hashes ACROSS all threads */
    int difficulty,
    int n_threads,
    char* out_hash_hex,
    uint64_t* out_nonce,
    uint64_t* out_hashes
) {
    if (!prefix || !diff_str || !out_hash_hex || !out_nonce || !out_hashes)
        return -1;
    if (difficulty < 1 || difficulty > 62)
        return -1;
    if (n_threads < 1) n_threads = 1;
    if (n_threads > 256) n_threads = 256;

    /* Single-thread → delega para a versão simples */
    if (n_threads == 1) {
        return brn_pow_search(prefix, prefix_len, diff_str, diff_str_len,
                              nonce_start, max_iters, difficulty,
                              out_hash_hex, out_nonce, out_hashes);
    }

    /* ---------- Setup compartilhado ---------- */
    atomic_int found;
    atomic_init(&found, 0);

    pthread_mutex_t result_lock;
    pthread_mutex_init(&result_lock, NULL);

    uint64_t result_nonce = 0;
    char result_hash[65];
    memset(result_hash, 0, sizeof(result_hash));
    int result_thread_id = -1;

    unsigned char target[32];
    make_target(difficulty, target);

    /* Quantos hashes por thread? (arredonda pra cima) */
    uint64_t total = max_iters;
    uint64_t per_thread = (total == 0)
                        ? 0
                        : (total + n_threads - 1) / n_threads;

    /* ---------- Lança threads ---------- */
    pthread_t* tids = (pthread_t*)malloc(sizeof(pthread_t) * n_threads);
    mt_worker_t* workers = (mt_worker_t*)malloc(sizeof(mt_worker_t) * n_threads);
    if (!tids || !workers) {
        free(tids); free(workers);
        pthread_mutex_destroy(&result_lock);
        return -1;
    }

    for (int i = 0; i < n_threads; i++) {
        workers[i].prefix = prefix;
        workers[i].prefix_len = prefix_len;
        workers[i].diff_str = diff_str;
        workers[i].diff_str_len = diff_str_len;
        memcpy(workers[i].target, target, 32);
        workers[i].thread_id = i;
        workers[i].n_threads = n_threads;
        workers[i].nonce_start = nonce_start;
        workers[i].max_hashes_per_thread = per_thread;
        workers[i].found = &found;
        workers[i].result_lock = &result_lock;
        workers[i].result_nonce = &result_nonce;
        workers[i].result_hash = result_hash;
        workers[i].result_thread_id = &result_thread_id;
        workers[i].hashes_done = 0;

        if (pthread_create(&tids[i], NULL, mt_worker, &workers[i]) != 0) {
            /* Falha em criar — espera as que já rodaram */
            for (int k = 0; k < i; k++) pthread_join(tids[k], NULL);
            free(tids); free(workers);
            pthread_mutex_destroy(&result_lock);
            return -1;
        }
    }

    /* ---------- Espera todas ---------- */
    for (int i = 0; i < n_threads; i++) {
        pthread_join(tids[i], NULL);
    }

    /* ---------- Total de hashes ---------- */
    uint64_t total_hashes = 0;
    for (int i = 0; i < n_threads; i++) {
        total_hashes += workers[i].hashes_done;
    }

    int found_flag = atomic_load(&found);

    /* ---------- Copia saída ---------- */
    if (found_flag) {
        memcpy(out_hash_hex, result_hash, 65);
        *out_nonce = result_nonce;
        *out_hashes = total_hashes;
    } else {
        out_hash_hex[0] = '\0';
        *out_nonce = nonce_start + total_hashes;  /* aproximação */
        *out_hashes = total_hashes;
    }

    free(tids);
    free(workers);
    pthread_mutex_destroy(&result_lock);

    return found_flag ? 0 : 1;
}


/* ============================================================
 * BENCHMARKS
 * ============================================================ */
static uint64_t _now_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}


double brn_benchmark(
    const char* prefix, size_t prefix_len,
    const char* diff_str, size_t diff_str_len,
    int difficulty, uint64_t iters
) {
    if (difficulty < 1 || difficulty > 62 || iters == 0) return 0.0;

    size_t buf_size = prefix_len + 24 + diff_str_len + 8;
    char* data = (char*)malloc(buf_size);
    if (!data) return 0.0;
    memcpy(data, prefix, prefix_len);

    unsigned char hash1[SHA256_DIGEST_LENGTH];
    unsigned char hash2[SHA256_DIGEST_LENGTH];

    for (uint64_t i = 0; i < 10000; i++) {
        int n = u64_to_dec(i, data + prefix_len);
        memcpy(data + prefix_len + n, diff_str, diff_str_len);
        SHA256((const unsigned char*)data, prefix_len + n + diff_str_len, hash1);
        SHA256(hash1, SHA256_DIGEST_LENGTH, hash2);
    }

    uint64_t t0 = _now_ns();
    for (uint64_t i = 0; i < iters; i++) {
        int n = u64_to_dec(i, data + prefix_len);
        memcpy(data + prefix_len + n, diff_str, diff_str_len);
        SHA256((const unsigned char*)data, prefix_len + n + diff_str_len, hash1);
        SHA256(hash1, SHA256_DIGEST_LENGTH, hash2);
    }
    uint64_t t1 = _now_ns();

    free(data);
    double dt = (t1 - t0) / 1e9;
    return (dt > 0) ? (double)iters / dt : 0.0;
}


double brn_benchmark_mt(
    const char* prefix, size_t prefix_len,
    const char* diff_str, size_t diff_str_len,
    int difficulty, uint64_t iters, int n_threads
) {
    if (difficulty < 1 || difficulty > 62 || iters == 0) return 0.0;
    if (n_threads < 1) n_threads = 1;

    uint64_t t0 = _now_ns();

    /* Usa brn_pow_search_mt com iters muito grande — não vai achar */
    /* (dificuldade alta para garantir que roda todos os iters) */
    char dummy_hex[65] = {0};
    uint64_t dummy_nonce = 0;
    uint64_t dummy_hashes = 0;

    brn_pow_search_mt(prefix, prefix_len, diff_str, diff_str_len,
                      0, iters, 60, n_threads,
                      dummy_hex, &dummy_nonce, &dummy_hashes);

    uint64_t t1 = _now_ns();
    double dt = (t1 - t0) / 1e9;
    return (dt > 0) ? (double)iters / dt : 0.0;
}


/* ============================================================
 * UTIL
 * ============================================================ */
int brn_n_cores(void) {
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    if (n < 1) n = 1;
    if (n > 256) n = 256;
    return (int)n;
}