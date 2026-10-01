# Сборка образа RAGFlow под arm64

[English version](BUILD.md)

RAGFlow публикует только x86-образы, поэтому RAGSpark собирает свой образ под
arm64 из исходников. Здесь описано, как это делается и чем сборка отличается
от официальной.

## Требования

- DGX Spark (нативный arm64, без эмуляции)
- Docker ≥ 24 с BuildKit
- около 50 GB свободного диска, 16 GB памяти; сборка идёт 30–60 минут

## Сборка

```bash
cd ragspark
sudo nohup build/ragflow/build.sh > ~/ragspark-build.log 2>&1 &
tail -f ~/ragspark-build.log
```

Запускайте в фоне: сборка на переднем плане отменяется при обрыве SSH.
Выход из `tail -f` через Ctrl+C сборку не останавливает.

Скрипт:

1. скачивает RAGFlow на теге `v0.27.2` и **проверяет коммит** (`a024bea0…`) —
   если тег когда-нибудь сдвинут, сборка остановится;
2. возвращает Dockerfile к исходному виду и применяет правку RAGSpark;
3. собирает `ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r1` с метками OCI
   (источник, версия, коммит upstream, лицензия);
4. с ключом `--push` отправляет образ в GHCR.

## Отличия от официальной сборки

Ровно одно, его вносит
[patch_dockerfile.py](../build/ragflow/patch_dockerfile.py): шаги установки
Chrome и ChromeDriver выполняются только на x86_64. Архивы в `ragflow_deps` —
x86-сборки, которые на arm64 не запускаются. На x86_64 исходные команды
выполняются без изменений.

**Следствие:** функции RAGFlow, которым нужен браузер (например, веб-краулер
в агентах), на arm64 недоступны. Разбор документов, поиск и чат это не
затрагивает.

## Ожидаемые предупреждения

| Сообщение | Что значит |
|---|---|
| `InvalidBaseImagePlatform: ... ragflow_deps ... linux/amd64` | `ragflow_deps` — образ только под x86, но сборка лишь копирует из него файлы данных. Безвредно. |
| `SecretsUsedInArgOrEnv ... GITEE_TOKEN` | Замечание линтера к официальному Dockerfile; переменная не используется. |
| `can't import package 'torch'` (при запуске) | В редакции slim нет встроенных моделей; их обслуживает vLLM. |

## Про сеть

Из некоторых сетей Docker Hub отвечает очень медленно: 1 октября 2026 года
рукопожатие TLS занимало 15–20 секунд, а Docker ждёт не больше 10
(`TLS handshake timeout`). GitHub, PyPI, GHCR и `mirror.gcr.io` в то же время
отвечали быстрее 0,2 секунды. Если вы столкнулись с этим, укажите зеркало
реестра в `/etc/docker/daemon.json`:

```json
{ "registry-mirrors": ["https://mirror.gcr.io"] }
```

и выполните `sudo systemctl restart docker`.

## Публикация в GHCR

1. Создайте **classic** personal access token с правом `write:packages`.
2. `sudo docker login ghcr.io -u <пользователь-github>` и вставьте токен.
3. `sudo build/ragflow/build.sh --push` или `sudo docker push <образ>` для уже
   собранного образа.
4. `sudo docker logout ghcr.io` — учётные данные хранятся незашифрованными.
5. На GitHub откройте настройки пакета и сделайте его публичным. Метка
   `org.opencontainers.image.source` связывает пакет с этим репозиторием.

Релизы ссылаются на образ по хешу, а не по тегу.
