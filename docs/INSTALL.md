# Installing RAGSpark

[Русская версия](INSTALL.ru.md)

The installer sets up the `single` mode on one DGX Spark, and the `stack`
mode on two (*Stack mode* below). It checks the
system, downloads what is missing, creates the settings with random passwords,
adds a system service that starts the stack at boot, starts it, registers the
models in RAGFlow and finishes with an acceptance test: a sample document is
indexed and the chat must answer questions about it with a citation. Decision
record: D18 in [DECISIONS.md](DECISIONS.md).

## Requirements

| | |
|---|---|
| Machine | NVIDIA DGX Spark (GB10, 128 GB unified memory) |
| System | DGX OS 7.x with Docker, the Compose plugin and the NVIDIA container runtime (all preinstalled) |
| Driver | NVIDIA 580.x |
| Python | 3.10 or newer (DGX OS ships 3.12) |
| Disk | about 60 GB for images and model weights, plus room for documents |
| Network | Docker Hub, `ghcr.io` and `huggingface.co` — or a registry mirror and weights copied in advance |
| Port | 80 free, or choose another one |

The web interface and the API are plain HTTP: install RAGSpark on a trusted
network. For access from outside it, put your own reverse proxy with TLS in
front of the web port.

## Install

```bash
git clone https://github.com/0ffch41n/ragspark.git
cd ragspark
sudo ./install.sh
```

The installer asks for the language, the superuser's e-mail
(`admin@ragspark.local` by default), the web port (80), the directory for model
weights (`/srv/ragspark/hf-cache`) and, when there is more than one, the LLM.
Then it checks the system, shows what it is going to create and asks for
confirmation. Nothing on the machine changes before that.

The LLM menu comes from [catalog/models.yaml](../catalog/models.yaml): speed
for one user, context, memory, tools and vision, license, and whether the model
passed the acceptance test (*validated*) or not yet (*experimental*). Enter its
number or name; Enter takes the default, which `--yes` takes as well. The plan
lists every model to be downloaded with its license. `./install.sh --llm list`
prints the menu without installing anything.

With the weights already downloaded the installation takes about 15 minutes,
10 of which the models need to start. A first install also downloads about
60 GB.

Without questions:

```bash
sudo ./install.sh --yes --lang en --admin-email admin@example.com --http-port 8080
```

| Option | Effect |
|---|---|
| `--lang ru\|en` | language of messages; also the language of the sample chat |
| `--yes`, `-y` | answer yes to every question, take the defaults |
| `--admin-email` | superuser e-mail |
| `--http-port`, `--http-bind` | web port and the address it listens on (default `0.0.0.0`) |
| `--models-dir` | directory for model weights |
| `--llm` | LLM file from `compose/llm/`, without `.yaml`; `--llm list` shows the models |
| `--role head\|node` | stack mode: `node` installs the second unit (*Stack mode*); `head`, the default, is a normal installation |
| `--head-ip`, `--node-ip` | stack mode: the cable addresses (default `192.168.100.10` and `192.168.100.11`) |
| `--skip-acceptance` | skip the acceptance test; the chat "Assistant" is still created |
| `--no-network` | skip the registry latency check |
| `--force` | go on despite failed checks (see *Checks*) |

`sudo` does not pass environment variables on. A mirror or a proxy for the
weights download go on the same command line; a Hugging Face token is read
without echo and handed over by name, so it appears neither in the process
list nor in the shell history:

```bash
sudo HF_ENDPOINT=https://hf-mirror.example HTTPS_PROXY=http://proxy:3128 ./install.sh
read -rs HF_TOKEN && export HF_TOKEN
sudo --preserve-env=HF_TOKEN ./install.sh
```

The default models are public and need no token. Docker pulls images through
its own settings; a registry mirror is described in [BUILD.md](BUILD.md).

## Steps

| # | Step | What happens |
|---|---|---|
| 1 | Checking the system | see *Checks* |
| 2 | Plan | what will be created; confirmation |
| 3 | Files | the installation goes to `/opt/ragspark`; the `ragspark` command |
| 4 | Settings and passwords | `compose/.env` with random passwords (mode 600), the chosen settings, `docker compose config` |
| 5 | Container images | missing images are pulled, three attempts each |
| 6 | GPU in a container | `nvidia-smi` in the vLLM image |
| 7 | Model weights | checked without network; missing ones downloaded |
| 8 | System service | `ragspark.service`, enabled |
| 9 | Pair watch timer | `ragspark-watch.timer`, enabled: every 30 s it keeps the two halves of a stack-mode LLM together, and does nothing in single mode (*Stack mode*) |
| 10 | Starting the stack | in order, about 10 minutes |
| 11 | Models in RAGFlow | LLM, embeddings and reranker registered and made the defaults |
| 12 | Acceptance test | see *Acceptance test* |

Weights are downloaded by a temporary container from the vLLM image, which
already has `huggingface_hub`; nothing is installed on the host. Only the
files vLLM loads are taken (`*.safetensors`, configs, tokenizer), not the
duplicate `*.bin` and `*.onnx` copies some repositories carry. Each model is
taken at the revision its compose file pins (`--revision`, the commit that was
validated), not at whatever the repository holds today. Weights already in the
directory are reused.

Every step can be repeated. If the installation stops — a download failed, the
machine rebooted, Ctrl+C — run `sudo ./install.sh` again: it continues with
the settings of the first run, and finished steps take seconds.

The log is `/opt/ragspark/install.log` (mode 600, no passwords in it).

## Checks

| Check | FAIL | WARN |
|---|---|---|
| Architecture, Python | not aarch64, Python older than 3.10 | |
| GPU, driver | no `nvidia-smi` | not GB10; driver not 580 |
| Memory | under 90 GiB | under 110 GiB |
| Docker, Compose | missing, older than 24.0 / 2.24 | |
| NVIDIA runtime | | not listed in Docker (checked again with a container in step 6) |
| Disk | under 25 GB free | under 100 GB free |
| Port | in use | |
| Registries | | not reachable; TLS handshake slower than 5 s (Docker gives up after 10 s) |
| Earlier installations | see below | |

`--force` continues despite FAIL, except for the checks that protect existing
data. These stop the installer in any case:

- `/opt/ragspark` or `/etc/systemd/system/ragspark.service` exists but was not
  created by the installer;
- containers of a RAGSpark stack started by hand (from a `compose/` directory)
  exist;
- volumes of an earlier stack exist: their databases accept only the passwords
  of that stack's `.env`.

The messages name the command that resolves each case. Removing volumes
deletes their documents, users and chats.

`sudo ./install.sh check` runs the checks alone.

## What is created

| Where | What |
|---|---|
| `/opt/ragspark` | `compose/`, `tools/`, `installer/`, `acceptance/`, `catalog/`; `compose/.env` with the passwords; `manifest.json`; `install.log` |
| `/srv/ragspark/hf-cache` | model weights (or the chosen directory) |
| `/etc/systemd/system/ragspark.service` | starts the stack at boot |
| `/etc/systemd/system/ragspark-watch.service`, `.timer` | runs `ragspark watch` every 30 s |
| `/usr/local/bin/ragspark` | the control command |
| Docker | project `ragspark`: 8 containers, 6 volumes, networks `ragspark` and `ragspark-backend`; the images |

Not changed: `/etc/docker/daemon.json`, system packages, the firewall, DNS,
other containers. `manifest.json` lists everything the installer created, and
uninstall removes exactly that.

After installation the clone of the repository is no longer needed. The stack
runs from `/opt/ragspark/compose`; do not start the `compose/` directory of
the clone as well — it is the same project `ragspark`.

## After installation

The installer prints the address, the login and the command that shows the
password:

```bash
sudo grep ^ADMIN_DEFAULT_PASSWORD= /opt/ragspark/compose/.env | cut -d= -f2
```

Two chats are ready:

| Chat | Knowledge base | For |
|---|---|---|
| "RAGSpark — пример" ("RAGSpark sample" with `--lang en`) | the sample memo | a working example with RAGSpark's settings |
| "Ассистент" ("Assistant" with `--lang en`) | none | general questions answered by the model alone; it knows neither your documents nor the internet |

The sample knowledge base and chat can be deleted in the web interface;
`sudo ragspark acceptance` creates them again and replaces a knowledge base or
chat with the same name. The assistant chat is created once and then left
alone, with its conversations; if it is deleted, `sudo ragspark acceptance`
creates it again.

New knowledge bases for Russian documents: language *Russian*, PDF parser
*Plain Text* (D14 in [DECISIONS.md](DECISIONS.md)). The models are already
the defaults.

For **scanned** PDFs — documents without a text layer — choose the vision model
`qwen3.8-27b` (instance `ragspark-vision`) as the PDF parser instead. It reads
every page as an image with the chat's own LLM: about 17 s a page, and chat
answers are slower while it indexes. Keep *Plain Text* for every PDF that has
a text layer: it is faster and reads the text exactly.

**Users.** Sign-up is off: the web interface hides it and the server rejects
registration requests too (`REGISTER_ENABLED=0`, checked in
[VALIDATION.md](VALIDATION.md), §11). New users are created by the superuser
through the admin service (`/api/v1/admin/users`), which answers only after
the superuser's login. Do not configure sign-in through an external provider
(OAuth, OIDC) without limiting who can use it: RAGFlow creates an account for
anyone who signs in through a configured provider, whether sign-up is on or
not.

### Reasoning levels

The selector under the chat input sets how the chat looks for the answer. It
starts at "Naive"; the browser remembers the choice.

| Level | How it answers | Time per answer¹ |
|---|---|---|
| Naive | one search with the chat's settings and one answer; if nothing passes the threshold, the chat's "no answer" text | 3–8 s |
| Low | RAGFlow's agentic pipeline: keywords from the question, a search, a check of the result | 15–20 s |
| Medium, High | the same with more searches, drafts and a review of whether the evidence is enough | about 1–2 min |
| Ultra | the most rounds | about 1–2 min |

¹ DGX Spark with the default model, on RAGSpark's question sets
([VALIDATION.md](VALIDATION.md), section 8).

Naive is enough when the answer is in one place of the documents; the higher
levels are for questions that need several passages put together. They write
the answer in their own words, so a question the documents do not answer may
get "the documents have no information on …" rather than the chat's "no
answer" text. Background: D19 in [DECISIONS.md](DECISIONS.md).

## Commands

| Command | Effect |
|---|---|
| `sudo ragspark status` | version, address, service, containers, RAGFlow health |
| `sudo ragspark start` | start and wait until every service is ready; starts only what is missing |
| `sudo ragspark stop` | stop; data is kept. The stack starts again at the next boot |
| `sudo ragspark restart` | stop and start, about 10 minutes |
| `sudo ragspark logs [service] [--tail N]` | follow the logs: `ragflow`, `vllm-llm`, `vllm-embed`, `vllm-rerank`, `es01`, `mysql`, `minio`, `redis` |
| `sudo ragspark llm [name]` | show the LLM, or change it: the chat waits about 7–10 minutes, the web interface about a minute; see [COMPOSE.md](COMPOSE.md), *Changing the LLM*. Also switches between single and stack mode (on the head) |
| `ragspark watch` | run by the timer every 30 s, not by hand: keeps the halves of a stack-mode LLM together, with the memory fuse (*Stack mode*) |
| `sudo ragspark acceptance` | run the acceptance test again |
| `sudo ragspark check [--gpu-reload]` | system check; see *GPU after a systemd reload* |
| `sudo ragspark uninstall` | remove; see *Uninstall* |

`--lang` and `--yes` work with every command.

## Start at boot

`ragspark.service` runs `docker compose up -d --wait` and does not hold up the
boot. RAGFlow starts after the models, so the web interface opens about
10 minutes after boot; `sudo ragspark status` shows the progress.

Docker starts some containers on its own when it starts: those whose last exit
code was not 0 (restart policy `on-failure`; a container killed after the stop
timeout exits with 137, Elasticsearch with 143; after a power loss every
container counts as failed). Started together, the models leave each other
without memory. The service therefore stops the containers that run without
being healthy and then starts everything in order.

## Acceptance test

[tools/acceptance.py](../tools/acceptance.py) runs inside the RAGFlow
container and goes through the whole chain:

1. creates the knowledge base with RAGSpark's settings (PDF text layer,
   chunks of 512 tokens) and uploads `acceptance/sample-ru.pdf`, a one-page
   fictional server memo;
2. waits for parsing and checks that the chunks are Russian text (at least
   80% Cyrillic letters);
3. creates the chat with the reranker, vector weight 0.7 and threshold 0.1
   (D15);
4. asks two questions with known answers — the answer must contain the
   expected facts, cite the document and contain no Chinese or Japanese
   characters — and one question the document does not answer, which must get
   the chat's "no answer" text;
5. asks the first question once more at the level Low, through the agentic
   pipeline, which must answer it the same way within 5 minutes;
6. creates the chat "Ассистент" if it does not exist and checks that it
   answers a simple question.

The test deletes the conversations it opened, so the chats start empty.

The PDF is generated reproducibly from `acceptance/sample-ru.md` by
`acceptance/make_sample.py`; the questions are in `acceptance/sample-ru.json`.

## GPU after a systemd reload

With Docker's `systemd` cgroup driver, `systemctl daemon-reload` (package
updates run it) can take the GPU away from running containers: `nvidia-smi`
inside them then fails with `Failed to initialize NVML: Unknown Error`. This is
a known NVIDIA Container Toolkit issue. The installer reloads systemd before
the models start and does not change Docker's settings. Test the machine:

```bash
sudo ragspark stop                  # the test reloads systemd; the stack must not run
sudo ragspark check --gpu-reload    # before installing: sudo ./install.sh check --gpu-reload
```

The test starts a container with the GPU, reloads systemd and checks the GPU
inside the container again. If it reports a loss, switch Docker to the
`cgroupfs` driver. This keeps the existing `/etc/docker/daemon.json` (DGX OS
registers the NVIDIA runtime there) and saves a copy:

```bash
sudo cp -a /etc/docker/daemon.json /etc/docker/daemon.json.bak
sudo python3 - <<'EOF'
import json
p = "/etc/docker/daemon.json"
cfg = json.load(open(p))
opts = [o for o in cfg.get("exec-opts", []) if not o.startswith("native.cgroupdriver=")]
cfg["exec-opts"] = opts + ["native.cgroupdriver=cgroupfs"]
json.dump(cfg, open(p, "w"), indent=2)
EOF
sudo systemctl restart docker       # stops every container on the machine
sudo docker info --format '{{.CgroupDriver}}'
sudo ragspark check --gpu-reload
sudo ragspark start
```

## Stack mode

Two DGX Spark units run one LLM that neither holds alone: DeepSeek V4 Flash,
about twice the speed of the default LLM, with the same answers on RAGSpark's
question sets (D10 in [DECISIONS.md](DECISIONS.md), [VALIDATION.md](VALIDATION.md),
sections 17–18). The first unit, the *head*, is a normal installation with
RAGFlow; the second, the *node*, runs the other half of the LLM, the embedding
model and the reranker. How the services are laid out: [COMPOSE.md](COMPOSE.md),
*Stack mode*.

**Requirements on top of the usual ones**

| | |
|---|---|
| Cable | one QSFP cable between the two units, set up as in NVIDIA's [guide for two Sparks](https://build.nvidia.com/spark/connect-two-sparks/stacked-sparks): 200 Gb/s, MTU 9000, both RoCE ports active |
| Addresses | `192.168.100.10` on the head, `192.168.100.11` on the node (others: `--head-ip`, `--node-ip`), kept across reboots |
| Memory | about 92 GiB on each unit for its half of the LLM |
| Disk | about 190 GB more on each unit: each reads its own copy of the weights (168 GB), plus the image |

**1. Install the node** — on the second unit, from a clone of the repository:

```bash
sudo ./install.sh --role node
```

It checks the cable (it stops if no interface holds the node's address),
downloads the images and the weights of the stack LLM, the embedding model and
the reranker, installs `ragspark.service` and the watch timer, and starts. The
embedding model and the reranker then answer on the cable, ports 8001 and
8002; the node's half of the LLM waits for the head. Nothing listens on the
local network. The node has no web interface and no data.

**2. Switch the head** — an installed head changes its LLM:

```bash
sudo ragspark llm deepseek-v4-flash-vision
```

A new head installs with it directly: `sudo ./install.sh --llm
deepseek-v4-flash-vision`, after the node. Before anything changes the command
checks the cable and the node's models; then the old LLM stops, the relays take
over the embedding model and the reranker (search pauses for about a minute)
and both halves load. The chat answers again after about 10 minutes the first
time; the command finishes with the model registration and the acceptance
test. Do not sign in to the web interface as the superuser while it runs:
RAGFlow keeps one session per user, and the command's registration fails with
HTTP 401. If that happens, run `sudo ragspark llm deepseek-v4-flash-vision`
again — it only finishes the registration — and then `sudo ragspark acceptance`.

**Back to one unit** — for maintenance of the node, or when it is down for
longer:

```bash
sudo ragspark llm qwen-3.8-27b-nvfp4
```

The embedding model and the reranker start on the head again. The node keeps
running and its half waits; `sudo ragspark stop` on the node stops it.

**Reboots and failures.** Each unit starts its own stack at boot, in any order;
the halves find each other. The web interface opens a minute or two after the
head boots, the chat after about 6 minutes. `ragspark-watch.timer` runs
`ragspark watch` on both units every 30 s and restarts a half that hangs or
lost its partner: a crash of one half was recovered without help in 6.5
minutes. While a unit is down, the chat, search and indexing do not work; the
web interface and the documents stay available.

**Memory fuse.** The same command stops — does not restart — its unit's half
when less than 4 GiB of memory is available on two runs in a row: a unit out
of memory does not fail, it stops responding until a reboot. The other half
then waits. `sudo ragspark status` shows when the fuse acted; find what took
the memory, then `sudo ragspark start` on that unit. The watch log:
`sudo journalctl -u ragspark-watch.service`.

**Port 29501.** In stack mode the head listens on port 29501 on every address:
the halves of the LLM meet there (torch's TCPStore, which cannot be bound to
one address). It is reachable from the local network while the LLM runs —
accepted on a trusted network (D12). The installer does not change the
firewall; to limit the port to the cable, on the head:

```bash
sudo iptables -I INPUT -p tcp --dport 29501 ! -s 192.168.100.0/24 -j DROP
sudo ip6tables -I INPUT -p tcp --dport 29501 -j DROP
```

The halves use IPv4 on the cable; with other cable addresses change the
network. The rules last until a reboot: keep them with your firewall's own
configuration, for example the `iptables-persistent` package.

**Uninstall** works the same on both units: `sudo ragspark uninstall`. Before
removing the node, switch the head back to one unit.

## Uninstall

```bash
sudo ragspark uninstall
```

Removes the containers and **all data** (documents, users, chats), the
service, the command, `/opt/ragspark`, and the images and model weights the
installer downloaded. Images, weights and directories that existed before the
installation stay. To reinstall without downloading again:

```bash
sudo ragspark uninstall --keep-images --keep-models
```

If Docker cannot remove the stack, uninstall stops and keeps
`/opt/ragspark/compose/.env`: it holds the only copy of the database
passwords.

## Troubleshooting

| Symptom | What to do |
|---|---|
| a check fails | the message says what to change; `sudo ./install.sh check` to repeat |
| image download fails or stalls | slow registry: a mirror ([BUILD.md](BUILD.md)); run the installer again, it continues |
| weights download fails | `HF_ENDPOINT` or a proxy on the `sudo` command line, a token through `--preserve-env=HF_TOKEN` (*Install*); or copy the weights into the directory in advance |
| step 10 fails | `sudo ragspark status`, then `sudo ragspark logs <service>`; [COMPOSE.md](COMPOSE.md), *Troubleshooting* |
| stack mode: the chat stopped answering | `sudo ragspark status` on both units; see *Stack mode* |
| acceptance test fails | its output names the failed check; `sudo ragspark logs ragflow`; repeat with `sudo ragspark acceptance` |
| an answer takes a minute or more | expected at Medium and above (see *Reasoning levels*); Naive answers in seconds |
| anything else | `/opt/ragspark/install.log` |

## Development

The installer is plain Python (standard library only) in
[installer/](../installer/). The tests in [tests/](../tests/) run it end to end
without a GPU or root: fake `docker`, `systemctl` and `nvidia-smi` commands
record what the installer does, `docker compose config` goes to a real Compose
binary, and a mock RAGFlow API serves the acceptance test.

```bash
RAGSPARK_TEST_COMPOSE=/path/to/docker-compose python3 -m unittest discover -s tests
```

Without `RAGSPARK_TEST_COMPOSE` the end-to-end tests are skipped.
