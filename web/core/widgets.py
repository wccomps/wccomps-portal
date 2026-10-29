from typing import cast

from django import forms


class ToggleButtonSelect(forms.Select):
    """Choices as toggle buttons over the hidden select the form posts. Unpicking resets a single select to its first
    choice, so that one is empty: it has no button. A page reacting to the choice listens for the select's change."""

    template_name = "forms/widgets/toggle_buttons.html"
    use_fieldset = True

    def id_for_label(self, id_: str, index: str | None = None) -> str:
        # The hidden select can't take focus: a link to the field goes to the picker around it
        return f"{id_}_0" if id_ else id_

    def use_required_attribute(self, initial: object) -> bool:
        # A hidden required select blocks the submit with nothing the user can see
        return False


class ToggleButtonSelectMultiple(ToggleButtonSelect, forms.SelectMultiple):
    """ToggleButtonSelect for any number of choices, with Select All and Deselect All buttons when select_all."""

    def __init__(self, attrs: dict[str, object] | None = None, select_all: bool = False) -> None:
        super().__init__(attrs)
        self.select_all = select_all

    def get_context(self, name: str, value: object, attrs: dict[str, object] | None) -> dict[str, object]:
        context = super().get_context(name, value, attrs)
        cast(dict[str, object], context["widget"])["select_all"] = self.select_all
        return context
