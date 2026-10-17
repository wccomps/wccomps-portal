from django import template

from core.utils import shorten

register = template.Library()
register.filter("shorten", shorten)
