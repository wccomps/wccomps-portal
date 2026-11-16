import re
from collections.abc import Iterator

from django.core.exceptions import ValidationError

# PostgreSQL's jsonb refuses a NUL character and a lone UTF-16 surrogate, both of which JSON text can carry
_JSONB_UNSTORABLE_RE = re.compile("[\x00\ud800-\udfff]")


def json_strings(value: object) -> Iterator[str]:
    """Every string in a JSON value: its texts and its objects' keys, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from json_strings(key)
            yield from json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_strings(item)


def validate_jsonb_storable(value: object) -> None:
    """ValidationError when PostgreSQL's jsonb can't store value: a text in it holds a NUL or a lone surrogate."""
    for text in json_strings(value):
        if _JSONB_UNSTORABLE_RE.search(text):
            raise ValidationError(
                "%(text)s holds a NUL character or a lone surrogate, which can't be stored.",
                params={"text": f"{text!r:.80}"},
            )
