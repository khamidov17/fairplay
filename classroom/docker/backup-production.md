# Production backup runbook

The `backup:prod` GitLab job is a separate manual job for `main`. Run it before
`deploy:prod`. It does not start the new image or run migrations.

## GitLab CI/CD variables

Create these variables with environment scope `production` and mark them
protected. Mask the two credential values and disable variable expansion for the
secret key.

- `BACKUP_S3_ENDPOINT_URL` — internal HTTP endpoint, for example `http://s3.school:3900`
- `BACKUP_S3_REGION`
- `BACKUP_S3_BUCKET`
- `BACKUP_S3_PREFIX` — optional; defaults to `dkuoj-production`
- `BACKUP_S3_ACCESS_KEY_ID`
- `BACKUP_S3_SECRET_ACCESS_KEY`

The S3 identity should be limited to list/read/write/delete within that prefix.
Credentials are streamed to a temporary file for the job and are not retained on
the production host or included in the backup.

## Output

Local backups are written to `/srv/dkuoj/backups/<UTC timestamp>-<commit>/` and
uploaded to `s3://<bucket>/<prefix>/<UTC timestamp>-<commit>/`. A remote backup is
complete only when it contains `_COMPLETE`. The newest ten complete backups are
retained in each location.

HTTP and unencrypted archives are intentional for the current school-network-only
deployment. Anyone able to observe that internal traffic can read the backup and
credentials; add TLS when the storage endpoint supports it.

## Restore rehearsal

Before relying on the workflow, restore one backup into an isolated development
environment:

1. Download every object except `_COMPLETE` and run `sha256sum -c SHA256SUMS`.
2. Extract the three data archives to their recorded paths from `metadata.env`.
3. Stop application writers before importing `database.sql.gz` into MySQL.
4. Start the previous SHA-tagged web and websocket images and run smoke tests.

For a production rollback, keep the site stopped between steps 2 and 4 so no
writes occur against a partially restored database or filesystem.
