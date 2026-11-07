"""
Tests for c-score_value Cotton component.

This component displays numeric score values with automatic color coding.
Tests verify rendering behavior through actual component usage rather than
checking template file contents.
"""

import pytest
from django.template import Context, Template
from django_cotton.compiler_regex import CottonCompiler

pytestmark = pytest.mark.django_db


class TestScoreValueComponentRendering:
    """Test c-score_value component rendering behavior."""

    def _render_component(self, value: str, fmt: str = "") -> str:
        """Render the score_value component with given parameters."""
        template_str = f'<c-score_value value="{value}" format="{fmt}" />'
        compiled = CottonCompiler().process(template_str)
        template = Template(compiled)
        return template.render(Context({}))

    def test_positive_value_renders_with_positive_class(self) -> None:
        """Positive values should render with score-positive class."""
        html = self._render_component("100")
        assert "score-positive" in html
        assert "100" in html

    def test_negative_value_renders_with_negative_class(self) -> None:
        """Negative values should render with score-negative class."""
        html = self._render_component("-50")
        assert "score-negative" in html
        assert "-50" in html

    def test_zero_value_renders_with_zero_class(self) -> None:
        """Zero values should render with score-zero class."""
        html = self._render_component("0")
        assert "score-zero" in html
        assert "0" in html

    def test_signed_format_adds_plus_to_positive(self) -> None:
        """Signed format should add + prefix to positive values."""
        html = self._render_component("75", "signed")
        # Template whitespace may separate + and number across lines
        normalized = " ".join(html.split())
        assert "+ 75" in normalized or "+75" in normalized

    def test_signed_format_keeps_minus_on_negative(self) -> None:
        """Signed format should keep - prefix on negative values."""
        html = self._render_component("-25", "signed")
        assert "-25" in html

    def test_renders_as_inline_element(self) -> None:
        """Component should render as inline element (span)."""
        html = self._render_component("100")
        assert "<span" in html
        assert "</span>" in html

    @pytest.mark.parametrize(
        "value,expected_class",
        [
            ("1", "score-positive"),
            ("999", "score-positive"),
            ("-1", "score-negative"),
            ("-999", "score-negative"),
            ("0", "score-zero"),
        ],
    )
    def test_various_values_get_correct_class(self, value: str, expected_class: str) -> None:
        """Various values should get the appropriate CSS class."""
        html = self._render_component(value)
        assert expected_class in html

    def test_a_score_shown_with_thousand_separators_keeps_its_class(self, settings) -> None:
        """floatformat writes -4720 as -4,720 when thousand separators are on; the class still reads it as negative."""
        settings.USE_THOUSAND_SEPARATOR = True
        html = self._render_component("-4720")
        assert "-4,720" in html
        assert "score-negative" in html

    def test_decimal_zero_renders_as_integer_zero(self) -> None:
        """Values like 0.00 or Decimal('0.00') should render as '0' without trailing decimals."""
        from decimal import Decimal

        template_str = '<c-score_value :value="val" />'
        compiled = CottonCompiler().process(template_str)
        template = Template(compiled)
        html = template.render(Context({"val": Decimal("0.00")}))
        assert "score-zero" in html
        assert ">0<" in html.replace(" ", "").replace("\n", "")
        assert "0.00" not in html

    def test_decimal_signed_renders_as_integer(self) -> None:
        """Signed decimal values with .00 should render without decimals."""
        from decimal import Decimal

        template_str = '<c-score_value :value="val" format="signed" />'
        compiled = CottonCompiler().process(template_str)
        template = Template(compiled)
        html = template.render(Context({"val": Decimal("15.00")}))
        assert "score-positive" in html
        assert "+15" in html.replace(" ", "").replace("\n", "")
        assert "15.00" not in html

    @pytest.mark.parametrize(
        ("value", "shown", "expected_class"),
        [
            ("0.6", "+1", "score-positive"),
            ("-0.6", "-1", "score-negative"),
            ("0.4", "0", "score-zero"),
            ("2.5", "+3", "score-positive"),
            ("-0.4", "0", "score-zero"),
            ("0.49999999999999999999999999999", "0", "score-zero"),
        ],
    )
    def test_the_class_and_sign_follow_the_rounded_value_shown(
        self, value: str, shown: str, expected_class: str
    ) -> None:
        """A score is shown as a whole number, so 0.6 shows as +1 and is colored as positive, not as zero."""
        html = self._render_component(value, "signed")
        assert expected_class in html
        assert f">{shown}<" in html.replace(" ", "").replace("\n", "")
