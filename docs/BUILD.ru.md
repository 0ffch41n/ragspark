# Сборка образа RAGFlow под arm64

[English version](BUILD.md)

RAGFlow публикует только x86-образы, поэтому RAGSpark собирает свой образ под
arm64 из исходников. Здесь описано, как это делается и чем сборка отличается
от официальной.

## Опубликованные образы

| Тег | Хеш | Правки | Статус |
|---|---|---|---|
| `0.27.2-arm64-r2` | публикуется после сборки | Chrome + текст для реранкера | текущий |
| `0.27.2-arm64-r1` | `sha256:1bf5fc0031bff2d775dc8bc1626b2820f9b658b43696a9bb4bdc02a8304f0660` | Chrome | заменён |

`r1`:

```bash
docker pull ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r1@sha256:1bf5fc0031bff2d775dc8bc1626b2820f9b658b43696a9bb4bdc02a8304f0660
```

`r1` проверен 1 октября 2026 года на DGX Spark: `/ragflow/VERSION` показывает
`v0.27.2`, каталога `/opt/chrome` нет, а с собственным compose RAGFlow
веб-интерфейс отвечает HTTP 200 и суперпользователь входит в систему.

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
2. возвращает исправляемые файлы к исходному виду и применяет правки RAGSpark;
3. собирает `ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r2` с метками OCI
   (источник, версия, коммит upstream, лицензия);
4. с ключом `--push` отправляет образ в GHCR.

## Отличия от официальной сборки

Два, каждое вносит скрипт, который останавливает сборку, если не находит
ожидаемый код (например, после смены версии RAGFlow):

1. [patch_dockerfile.py](../build/ragflow/patch_dockerfile.py) — шаги установки
   Chrome и ChromeDriver выполняются только на x86_64. Архивы в
   `ragflow_deps` — x86-сборки, которые на arm64 не запускаются. На x86_64
   исходные команды выполняются без изменений. **Следствие:** функции RAGFlow,
   которым нужен браузер (например, веб-краулер в агентах), на arm64
   недоступны.
2. [patch_search.py](../build/ragflow/patch_search.py) — реранкер получает
   исходный текст фрагмента вместо `remove_redundant_spaces(" ".join(tokens))`,
   которая склеивает кириллицу и другие нелатинские алфавиты в одно слово и
   занижает оценки реранкера в 2–3 раза. Подробности и замеры:
   [VALIDATION.ru.md](VALIDATION.ru.md), раздел 4.

## Ожидаемые предупреждения

| Сообщение | Что значит |
|---|---|
| `InvalidBaseImagePlatform: ... ragflow_deps ... linux/amd64` | `ragflow_deps` — образ только под x86, но сборка лишь копирует из него файлы данных. Безвредно. |
| `SecretsUsedInArgOrEnv ... GITEE_TOKEN` | Замечание линтера к официальному Dockerfile; переменная не используется. |
| `can't import package 'torch'` (при запуске) | В редакции slim нет встроенных моделей; их обслуживает vLLM. |

## Стороннее ПО внутри образа

Официальный Dockerfile среди прочих пакетов ставит драйвер Microsoft ODBC
для SQL Server (`msodbcsql18` на arm64, `msodbcsql17` на x86) и принимает его
лицензию во время сборки; официальные образы RAGFlow распространяют
x86-вариант. Условия его распространения определяет Microsoft — см. открытые
вопросы в [DECISIONS.ru.md](DECISIONS.ru.md).

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
