"""The input strategies post what a browser posts: strings (or lists of them) and uploaded files."""

import pytest
from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from hypothesis import given
from hypothesis import strategies as st

from core.tests.strategies import field_values, valid_values

FIELDS = (
    forms.CharField(max_length=20),
    forms.EmailField(),
    forms.IntegerField(min_value=0, max_value=10),
    forms.DecimalField(max_digits=5, decimal_places=2),
    forms.DateField(),
    forms.DateTimeField(),
    forms.TimeField(),
    forms.BooleanField(),
    forms.GenericIPAddressField(),
    forms.ChoiceField(choices=[("a", "A")]),
    forms.MultipleChoiceField(choices=[("a", "A")]),
    forms.FileField(),
)


@pytest.mark.parametrize("field", FIELDS, ids=lambda f: type(f).__name__)
@given(data=st.data(), valid=st.booleans())
def test_every_field_strategy_draws_what_a_browser_posts(field: forms.Field, data: st.DataObject, valid: bool) -> None:
    value = data.draw(valid_values(field) if valid else field_values(field))
    posted = value if isinstance(value, list) else [value]
    assert all(isinstance(v, (str, SimpleUploadedFile)) for v in posted), value
