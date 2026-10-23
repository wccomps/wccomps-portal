from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.auth_utils import require_permission

from ..forms import ScoringTemplateForm
from ..models import ScoringTemplate
from ..quotient_sync import sync_service_scores


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def scoring_config(request: HttpRequest) -> HttpResponse:
    template = ScoringTemplate.objects.first()
    if not template:
        template = ScoringTemplate.objects.create()

    if request.method == "POST":
        form = ScoringTemplateForm(request.POST, instance=template)
        if form.is_valid():
            template = form.save(commit=False)
            template.updated_by = cast(User, request.user)
            template.save()
            messages.success(request, "Scoring configuration updated")
            return redirect("scoring:scoring_config")
    else:
        form = ScoringTemplateForm(instance=template)

    context = {
        "form": form,
        "template": template,
    }
    return render(request, "scoring/scoring_config.html", context)


@require_permission("gold_team", error_message="Only Gold Team members can access this")
@require_http_methods(["POST"])
def sync_scores(request: HttpRequest) -> HttpResponse:
    try:
        result = sync_service_scores(cast(User, request.user))
        messages.success(request, f"Synced {result['total']} teams")
    except Exception as e:
        messages.error(request, f"Failed to sync scores: {e}")
    return redirect("scoring:scoring_config")
