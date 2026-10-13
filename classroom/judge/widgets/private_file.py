from django.forms import ClearableFileInput

__all__ = ['PrivateFileInput']


class PrivateFileInput(ClearableFileInput):
    """File input for fields stored in private object storage.

    ``ClearableFileInput`` renders the current value as ``<a href="{{ value.url }}">``.
    Files kept in ``judge.storage.submission_storage`` have no usable URL — the S3
    backend runs with ``querystring_auth=False`` against a private bucket, and the
    dev/CI ``FileSystemStorage`` has no ``base_url`` at all and raises ``ValueError``
    from ``.url``. Rendering the stock widget therefore 500s the admin change form
    for any row that already has a file.

    Suppressing the "currently" block keeps the upload control working; a readonly
    admin field provides a real, permission-checked download link instead.
    """

    def is_initial(self, value):
        return False
