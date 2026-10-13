"""JSON that is safe to place inside an inline ``<script>`` block."""
import json

from django.utils.safestring import mark_safe

_SCRIPT_ESCAPES = {
    ord('<'): '\\u003c',
    ord('>'): '\\u003e',
    ord('&'): '\\u0026',
    ord(' '): '\\u2028',
    ord(' '): '\\u2029',
}


def script_json(value, **kwargs):
    """``json.dumps(value)`` with ``<``, ``>``, ``&``, U+2028 and U+2029 escaped, marked safe.

    The result can be written as a JavaScript literal (``var x = {{ data }};``) or into a
    ``<script type="application/json">`` element: no value can close the script element
    (``</script>``), open an HTML comment (``<!--``) or break a JavaScript string literal.
    """
    return mark_safe(json.dumps(value, **kwargs).translate(_SCRIPT_ESCAPES))
