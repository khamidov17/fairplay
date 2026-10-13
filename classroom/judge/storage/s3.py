from botocore.client import Config
from django.conf import settings
from storages.backends.s3 import S3Storage


class SubmissionFileStorage(S3Storage):
    """Primary submission-file store on any S3-compatible endpoint (Garage in production).

    Timeouts are deliberately tight so a hung object store fails a web request in
    ~10s (5s connect x 2 attempts) instead of hanging it; the ResilientStorage
    wrapper turns those failures into backup reads.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault('endpoint_url', settings.DMOJ_S3_ENDPOINT_URL)
        kwargs.setdefault('bucket_name', settings.DMOJ_S3_SUBMISSION_BUCKET)
        kwargs.setdefault('access_key', settings.DMOJ_S3_ACCESS_KEY)
        kwargs.setdefault('secret_key', settings.DMOJ_S3_SECRET_KEY)
        kwargs.setdefault('region_name', settings.DMOJ_S3_REGION)
        kwargs.setdefault('addressing_style', 'path')  # self-hosted stores have no wildcard DNS
        kwargs.setdefault('signature_version', 's3v4')
        kwargs.setdefault('default_acl', None)  # Garage permissions are key-based, not ACLs
        kwargs.setdefault('file_overwrite', False)
        kwargs.setdefault('querystring_auth', False)  # downloads stream through Django, not signed URLs
        kwargs.setdefault('client_config', Config(
            connect_timeout=5,
            read_timeout=30,
            retries={'max_attempts': 2, 'mode': 'standard'},
        ))
        super().__init__(**kwargs)
