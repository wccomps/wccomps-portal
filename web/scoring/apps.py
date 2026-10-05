from django.apps import AppConfig


class ScoringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scoring"

    def ready(self) -> None:
        from django.db.models.signals import post_delete, post_save
        from orange_team.models import OrangeCheck, OrangeCheckCriterion

        from scoring.signals import recompute_orange_max

        for model in (OrangeCheck, OrangeCheckCriterion):
            name = model.__name__.lower()
            post_save.connect(recompute_orange_max, sender=model, dispatch_uid=f"orange_max_{name}_save")
            post_delete.connect(recompute_orange_max, sender=model, dispatch_uid=f"orange_max_{name}_delete")
