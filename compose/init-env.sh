#!/usr/bin/env bash
# Create compose/.env from env.example: random passwords, host time zone.
#
#   ./init-env.sh
#
# Never overwrites an existing .env: the databases keep the passwords they
# were created with, so a new .env would lock RAGFlow out of its own data.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/env.example"
TARGET="$HERE/.env"

die() { echo "init-env: $*" >&2; exit 1; }

[[ -f "$TEMPLATE" ]] || die "$TEMPLATE not found"
if [[ -e "$TARGET" ]]; then
    die "$TARGET already exists. It holds the passwords the databases were created
with; remove it only together with the data (docker compose down -v)."
fi

# Random hex: safe in YAML, URLs and shell, which RAGFlow's config template needs.
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
if [[ -z "$tz" && -L /etc/localtime ]]; then
    tz="$(readlink -f /etc/localtime | sed -n 's#^.*/zoneinfo/##p')"
fi
[[ "$tz" =~ ^[A-Za-z0-9_+/-]+$ ]] || tz="Etc/UTC"

umask 077
tmp="$(mktemp "$HERE/.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//@SECRET64@/$(hex 32)}"
    line="${line//@SECRET@/$(hex 16)}"
    line="${line//@TZ@/$tz}"
    printf '%s\n' "$line"
done < "$TEMPLATE" > "$tmp"
grep -q '@SECRET' "$tmp" && die "unreplaced placeholder in $TEMPLATE"
mv "$tmp" "$TARGET"
trap - EXIT

email="$(sed -n 's/^DEFAULT_SUPERUSER_EMAIL=//p' "$TARGET")"
echo "init-env: created $TARGET (mode 600)"
echo "  time zone : $tz"
echo "  superuser : $email"
echo "  password  : grep ^ADMIN_DEFAULT_PASSWORD= $TARGET"
echo "Edit DEFAULT_SUPERUSER_EMAIL or RAGSPARK_HTTP_PORT now if needed: the superuser is created on the first start."
