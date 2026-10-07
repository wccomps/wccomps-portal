"""Tests for c-link Cotton component."""

import pytest
from django.template import Context, Template
from django_cotton.compiler_regex import CottonCompiler

pytestmark = pytest.mark.django_db


class TestCottonLinkComponent:
    """Test c-link rendering behavior."""

    def _render(self, template_str: str) -> str:
        compiled = CottonCompiler().process(template_str)
        return Template(compiled).render(Context({}))

    def test_primary_variant_renders_button_role(self) -> None:
        """variant='primary' renders class='button default' and role='button'."""
        html = self._render('<c-link href="/test" variant="primary">Download</c-link>')
        assert 'class="button default "' in html
        assert 'role="button"' in html

    def test_danger_variant_renders_button_role(self) -> None:
        """variant='danger' renders class='button warning' and role='button'."""
        html = self._render('<c-link href="/test" variant="danger">Delete</c-link>')
        assert 'class="button warning"' in html
        assert 'role="button"' in html

    def test_button_class_renders_button_role(self) -> None:
        """Explicit class='button' renders role='button'."""
        html = self._render('<c-link href="/test" class="button">Custom</c-link>')
        assert 'class="button"' in html
        assert 'role="button"' in html

    def test_plain_link_does_not_render_button_role(self) -> None:
        """Plain links should not have role='button'."""
        html = self._render('<c-link href="/test">Plain</c-link>')
        assert 'role="button"' not in html

    def test_explicit_role_is_preserved(self) -> None:
        """Explicit role should be preserved and not overwritten."""
        html = self._render('<c-link href="/test" role="tab">Tab</c-link>')
        assert 'role="tab"' in html
        assert 'role="button"' not in html
