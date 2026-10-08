# Running RAGSpark with Docker Compose

[Русская версия](COMPOSE.ru.md)

The [compose/](../compose/) directory runs the whole `single` mode on one DGX
Spark: RAGFlow, its databases and the three vLLM model servers. The installer
(stage 3.3) will wrap the steps below; until then they are run by hand.
Decision record: D17 in [DECISIONS.md](DECISIONS.md).

## Files

| File | Purpose |
|---|---|
| `compose/compose.yaml` | all services except the LLM's image and flags |
| `compose/llm/<model>.yaml` | one file per LLM: image, vLLM flags, what RAGFlow registers |
| `compose/env.example` | settings template; `init-env.sh` turns it into `.env` |
| `compose/init-env.sh` | creates `.env` with random passwords and the host time zone |
| `compose/.env` | this install's settings and passwords (mode 600, not in git) |

`COMPOSE_FILE` in `.env` joins `compose.yaml` with the selected LLM file, so
every `docker compose` command must run from the `compose/` directory.

## Services

| Service | Image | Role | Memory |
|---|---|---|---|
| `vllm-embed` | vLLM 0.27.1 | embeddings, `deepvk/USER-bge-m3` | 5% of unified memory |
| `vllm-rerank` | vLLM 0.27.1 | reranker, `BAAI/bge-reranker-v2-m3` | 3% |
| `vllm-llm` | from the LLM file | chat model (default Qwen 3.8 27B NVFP4) | 50% |
| `es01` | Elasticsearch 8.11.3 | full-text and vector index | up to 8 GB |
| `mysql` | MySQL 8.0.40 | RAGFlow metadata | — |
| `minio` | pgsty/silo | uploaded files | — |
| `redis` | Valkey 8 | queues and cache | 128 MB |
| `ragflow` | RAGSpark r2 | web interface, API, document processing | — |

Only `ragflow` publishes a port: its nginx on port 80 (`RAGSPARK_HTTP_BIND`,
`RAGSPARK_HTTP_PORT`), which serves both the web interface and the API under
`/api/v1`. The other services are reachable only on the internal network
`ragspark`, by service name.

## Start order

1. `vllm-embed`, then `vllm-rerank`, then `vllm-llm` — one at a time, each
   waiting for the previous one to report healthy.
2. `es01`, `mysql`, `minio` and `redis` start at once, in parallel with the
   models.
3. `ragflow` starts when all of the above are healthy, so it never sends a
   request to a model that is still loading.

The models start one at a time because vLLM sizes its cache from the memory it
measures while starting. On unified memory, memory taken by another model
starting in the same window is counted against it, and the smaller models are
left with no room for their cache.

Measured on 2026-10-08 with the weights already in the cache: 9 min 10 s for
the whole stack — embeddings 36 s, reranker 36 s, LLM 7 min 11 s, RAGFlow
about 45 s. Restarting the LLM alone takes about 5.5 minutes, because the
compile cache is kept between starts.

## First start

Requirements: Docker with the NVIDIA runtime (DGX OS ships both), the images
present locally and the model weights in `/srv/ragspark/hf-cache`.

```bash
cd compose
./init-env.sh                       # creates .env; never overwrites it
sudo docker compose config --quiet  # checks the files and .env
sudo docker compose up -d --wait --wait-timeout 2400
sudo docker compose ps
```

`init-env.sh` sets the superuser to `admin@ragspark.local`; edit
`DEFAULT_SUPERUSER_EMAIL` in `.env` before the first start if you want another
address. The password is `ADMIN_DEFAULT_PASSWORD` in `.env`. Both are read once,
when RAGFlow creates the superuser.

Then register the models in RAGFlow and make them the defaults:

```bash
sudo docker compose exec -T ragflow \
  sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python -' < ../tools/ragflow_models.py
```

and open `http://<spark address>/` in a browser.

**Weights not in the cache yet?** vLLM starts offline (`RAGSPARK_HF_OFFLINE=1`)
and stops if a file is missing. For a first download set
`RAGSPARK_HF_OFFLINE=0` in `.env`, start, and set it back to `1` afterwards.

## Everyday commands

All from `compose/`:

| Task | Command |
|---|---|
| status | `sudo docker compose ps` |
| logs of one service | `sudo docker compose logs -f --tail 100 ragflow` |
| stop everything, keep data | `sudo docker compose down` |
| start again | `sudo docker compose up -d --wait --wait-timeout 2400` |
| restart one service | `sudo docker compose restart vllm-llm` |
| shell in RAGFlow | `sudo docker compose exec ragflow bash` |

Containers restart after a crash, but **nothing starts by itself when the
Spark boots**: started by Docker itself, all containers would come up at
once and break the start order. Run `up` after a reboot; the installer will add a systemd unit for it.

## Changing the LLM

Point `COMPOSE_FILE` in `.env` at another file in `compose/llm/` and run
`sudo docker compose up -d --wait`, then `tools/ragflow_models.py` to register
the new model. Today the directory holds the default model only; more come
with stage 4.

## Data

| Volume | Contents |
|---|---|
| `ragspark_esdata` | search index |
| `ragspark_mysql` | users, knowledge bases, chats, settings |
| `ragspark_minio` | uploaded documents |
| `ragspark_valkey` | task queues and cache |
| `ragspark_ragflow-logs` | RAGFlow logs |
| `ragspark_vllm-compile-cache` | vLLM compile results, speeds up later starts |
| `/srv/ragspark/hf-cache` (host directory) | model weights |

`sudo docker compose down -v` deletes every volume listed above — all
documents, users and chats. The weights in `/srv/ragspark/hf-cache` stay.
Keep `.env` as long as the volumes exist: the databases only accept the
passwords they were created with.

Each container's log is capped at 5 files of 20 MB.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `up` stops with `dependency failed to start` | a model or database did not become healthy: `sudo docker compose ps -a` and the logs of that service |
| a vLLM service exits with `Free memory on device … is less than desired` | other processes or the file cache hold the memory. Check `free -g`; free the cache with `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` |
| a vLLM service exits with `LocalEntryNotFoundError` or `offline mode` | weights missing from the cache — see *Weights not in the cache yet?* |
| port 80 already in use | another web server or an old stack; change `RAGSPARK_HTTP_PORT` or stop it |
