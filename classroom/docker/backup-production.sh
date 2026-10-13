#!/bin/sh
# Run on the production Docker host via the manual GitLab backup:prod job.
set -eu

umask 077

: "${DKUOJ_IMAGE:?missing DKUOJ_IMAGE}"
: "${BACKUP_S3_ENV:?missing BACKUP_S3_ENV}"

site_dir=${DATA_DIR:-/srv/dkuoj}
judge_dir=${JUDGE_DATA_DIR:-/srv/dkuoj-judge}
problem_dir=${PROBLEM_DATA_ROOT:-/mnt/problems}
backup_root=${BACKUP_ROOT:-$site_dir/backups}
keep=${BACKUP_KEEP:-10}
commit=${CI_COMMIT_SHORT_SHA:-unknown}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_id="$timestamp-$commit"
staging="$backup_root/.incomplete-$backup_id"
destination="$backup_root/$backup_id"
lock_dir="$backup_root/.backup.lock"
complete=false

case "$keep" in *[!0-9]*|'') echo 'BACKUP_KEEP must be a positive integer' >&2; exit 2 ;; esac
[ "$keep" -gt 0 ] || { echo 'BACKUP_KEEP must be greater than zero' >&2; exit 2; }
[ -f "$site_dir/local_settings.py" ] || { echo "missing $site_dir/local_settings.py" >&2; exit 1; }
[ -f "$site_dir/.env" ] || { echo "missing $site_dir/.env" >&2; exit 1; }
[ -d "$judge_dir" ] || { echo "missing $judge_dir" >&2; exit 1; }
[ -d "$problem_dir" ] || { echo "missing $problem_dir" >&2; exit 1; }
[ -f "$BACKUP_S3_ENV" ] || { echo 'temporary S3 environment file is missing' >&2; exit 1; }

mkdir -p "$backup_root"
if ! mkdir "$lock_dir" 2>/dev/null; then
    echo "another production backup is already running ($lock_dir exists)" >&2
    exit 1
fi
cleanup() {
    rmdir "$lock_dir" 2>/dev/null || true
    if [ "$complete" != true ]; then
        rm -rf "$staging"
    fi
}
trap cleanup EXIT HUP INT TERM

[ ! -e "$destination" ] || { echo "backup already exists: $destination" >&2; exit 1; }
mkdir "$staging"

# A full tarball can be almost as large as its source. Reserve the source size
# plus 10% and 1 GiB for the database dump and compression overhead.
source_kb=$(du -sk "$site_dir/static" "$site_dir/media" "$judge_dir" "$problem_dir" 2>/dev/null \
    | awk '{total += $1} END {print total + 0}')
available_kb=$(df -Pk "$backup_root" | awk 'NR == 2 {print $4}')
required_kb=$((source_kb + source_kb / 10 + 1048576))
if [ "$available_kb" -lt "$required_kb" ]; then
    echo "insufficient backup space: need ${required_kb} KiB, have ${available_kb} KiB" >&2
    exit 1
fi

echo "Pulling backup tooling image $DKUOJ_IMAGE"
docker pull "$DKUOJ_IMAGE"

echo 'Creating database dump'
docker run --rm \
    --add-host host.docker.internal:host-gateway \
    --user "$(id -u):$(id -g)" \
    --env-file "$site_dir/.env" \
    -v "$site_dir/local_settings.py:/app/dmoj/local_settings.py:ro" \
    -v "$staging:/backup" \
    --entrypoint python \
    "$DKUOJ_IMAGE" manage.py dump_database --output /backup/database.sql.gz

echo 'Archiving site data'
# The site/judge/problem trees hold root-owned, non-searchable directories
# written by the containers (they run as root), which host-side tar running as
# the deploy user cannot stat. Archive inside a root container instead, then
# hand the results back to the invoking user so the verify, move and prune
# steps below still work under this script's umask.
docker run --rm \
    --user 0:0 \
    -e OWNER="$(id -u):$(id -g)" \
    -e PROBLEM_BASE="$(basename "$problem_dir")" \
    -v "$site_dir:/data/site:ro" \
    -v "$judge_dir:/data/judge:ro" \
    -v "$(dirname "$problem_dir"):/data/problem-parent:ro" \
    -v "$staging:/backup" \
    --entrypoint sh \
    "$DKUOJ_IMAGE" -euc '
        tar -czf /backup/site-data.tar.gz \
            -C /data/site .env local_settings.py docker-compose.yml static media
        tar -czf /backup/judge-data.tar.gz -C /data/judge .
        tar -czf /backup/problem-data.tar.gz -C /data/problem-parent "$PROBLEM_BASE"
        chown -R "$OWNER" /backup
    '

cat > "$staging/metadata.env" <<EOF
BACKUP_ID=$backup_id
CREATED_AT_UTC=$timestamp
CI_COMMIT_SHORT_SHA=$commit
DKUOJ_IMAGE=$DKUOJ_IMAGE
SITE_DATA_DIR=$site_dir
JUDGE_DATA_DIR=$judge_dir
PROBLEM_DATA_ROOT=$problem_dir
EOF

for archive in "$staging"/*.gz; do gzip -t "$archive"; done
(cd "$staging" && sha256sum ./*.gz metadata.env > SHA256SUMS && sha256sum -c SHA256SUMS)

mv "$staging" "$destination"
complete=true

echo 'Uploading verified snapshot to S3'
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    --env-file "$BACKUP_S3_ENV" \
    -v "$destination:/backup:ro" \
    --entrypoint /app/docker/s3-backup-upload.sh \
    "$DKUOJ_IMAGE" /backup "$backup_id"

# Prune only completed timestamped directories, and only after S3 succeeds.
old_backups=$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d \
    -name '20??????T??????Z-[0-9a-f]*' -printf '%f\n' | sort -r | sed -n "$((keep + 1)),\$p")
printf '%s\n' "$old_backups" | while IFS= read -r old_backup; do
    [ -n "$old_backup" ] || continue
    rm -rf "$backup_root/$old_backup"
done

echo "Production backup complete: $destination"
