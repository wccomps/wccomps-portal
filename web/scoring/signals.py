"""Keep ScoringTemplate.orange_max current from the orange check definitions.

Orange checks (and their criteria) are edited on the lead taskboard and in the admin;
unlike service/inject maxima there is no sync action to hang the derivation on, so a
signal recomputes orange_max whenever a check or criterion changes.
"""

from scoring.maxes import orange_max_from_checks, set_template_max


def recompute_orange_max(**kwargs: object) -> None:
    if kwargs.get("raw"):
        return  # loaddata: don't overwrite a fixture-provided orange_max from partial data
    set_template_max("orange_max", orange_max_from_checks())
