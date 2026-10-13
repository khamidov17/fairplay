#!/bin/sh
# Upload one completed backup directory to an S3-compatible service, then keep
# the ten newest completed remote backups. Intended to run inside the web image.
set -eu

backup_dir=${1:?usage: s3-backup-upload.sh BACKUP_DIR BACKUP_ID}
backup_id=${2:?usage: s3-backup-upload.sh BACKUP_DIR BACKUP_ID}

: "${BACKUP_S3_ENDPOINT_URL:?missing BACKUP_S3_ENDPOINT_URL}"
: "${BACKUP_S3_REGION:?missing BACKUP_S3_REGION}"
: "${BACKUP_S3_BUCKET:?missing BACKUP_S3_BUCKET}"
: "${BACKUP_S3_ACCESS_KEY_ID:?missing BACKUP_S3_ACCESS_KEY_ID}"
: "${BACKUP_S3_SECRET_ACCESS_KEY:?missing BACKUP_S3_SECRET_ACCESS_KEY}"

case "$backup_id" in
    20[0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f]*) ;;
    *) echo "invalid backup id: $backup_id" >&2; exit 2 ;;
esac

prefix=${BACKUP_S3_PREFIX:-dkuoj-production}
prefix=${prefix#/}
prefix=${prefix%/}
[ -n "$prefix" ] || { echo 'BACKUP_S3_PREFIX must not be empty' >&2; exit 2; }
case "$prefix" in
    *[!A-Za-z0-9._/-]*) echo "invalid S3 prefix: $prefix" >&2; exit 2 ;;
esac

[ -d "$backup_dir" ] || { echo "backup directory not found: $backup_dir" >&2; exit 2; }

export AWS_ACCESS_KEY_ID=$BACKUP_S3_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY=$BACKUP_S3_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION=$BACKUP_S3_REGION
export AWS_EC2_METADATA_DISABLED=true

aws_internal() {
    aws --endpoint-url "$BACKUP_S3_ENDPOINT_URL" --region "$BACKUP_S3_REGION" "$@"
}

remote_base="s3://${BACKUP_S3_BUCKET}/${prefix}/${backup_id}"
for path in "$backup_dir"/*; do
    [ -f "$path" ] || continue
    name=${path##*/}
    aws_internal s3 cp "$path" "$remote_base/$name" --no-progress
    local_size=$(wc -c < "$path" | tr -d ' ')
    remote_size=$(aws_internal s3api head-object \
        --bucket "$BACKUP_S3_BUCKET" \
        --key "$prefix/$backup_id/$name" \
        --query ContentLength --output text)
    [ "$local_size" = "$remote_size" ] || {
        echo "remote size mismatch for $name ($local_size != $remote_size)" >&2
        exit 1
    }
done

marker=$(mktemp)
trap 'rm -f "$marker"' EXIT HUP INT TERM
printf '%s\n' "$backup_id" > "$marker"
aws_internal s3 cp "$marker" "$remote_base/_COMPLETE" --no-progress

# Timestamp-first backup IDs sort chronologically. Only _COMPLETE markers count;
# partial uploads are never allowed to evict a valid backup.
markers=$(aws_internal s3api list-objects-v2 \
    --bucket "$BACKUP_S3_BUCKET" \
    --prefix "$prefix/" \
    --query 'Contents[?ends_with(Key, `/_COMPLETE`)].Key' \
    --output text | tr '\t' '\n' | sed '/^$/d' | sort -r)
old_markers=$(printf '%s\n' "$markers" | sed -n '11,$p')
printf '%s\n' "$old_markers" | while IFS= read -r old_marker; do
    [ -n "$old_marker" ] || continue
    old_prefix=${old_marker%/_COMPLETE}
    case "$old_prefix" in
        "$prefix"/20[0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f]*)
            aws_internal s3 rm "s3://${BACKUP_S3_BUCKET}/$old_prefix/" --recursive --no-progress
            ;;
        *)
            echo "refusing to prune unexpected S3 prefix: $old_prefix" >&2
            exit 1
            ;;
    esac
done

echo "S3 backup complete: $remote_base"
