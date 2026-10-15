from django import template

from core.page_titles import PAGE_TITLES

register = template.Library()


@register.simple_tag
def page_title(url_name: str) -> str:
    """The page's title, as its header and the links that name it show it."""
    return PAGE_TITLES[url_name]
