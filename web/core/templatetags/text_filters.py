from django import template

from core.form_errors import fields_with_errors, top_errors
from core.utils import shorten

register = template.Library()
register.filter("shorten", shorten)
register.filter("top_errors", top_errors)
register.filter("fields_with_errors", fields_with_errors)
