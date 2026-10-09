from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from core.forms import CategoryForm

from ..auth_utils import require_permission


@require_permission("gold_team", "ticketing_admin")
def admin_categories(request: HttpRequest) -> HttpResponse:
    from ticketing.models import TicketCategory

    categories = TicketCategory.objects.all()
    return render(request, "admin/categories.html", {"categories": categories})


@require_permission("gold_team", "ticketing_admin")
def admin_category_create(request: HttpRequest) -> HttpResponse:
    from ticketing.models import TicketCategory

    if request.method == "POST":
        form = CategoryForm(request.POST)
        if not form.is_valid():
            error_msg = (
                "Display name is required."
                if "display_name" in form.errors
                else str(next(iter(form.errors.values()))[0])
            )
            return render(
                request,
                "admin/category_form.html",
                {
                    "error": error_msg,
                    "form_data": request.POST,
                },
            )

        TicketCategory.objects.create(
            display_name=form.cleaned_data["display_name"],
            points=form.cleaned_data["points"],
            playbook=form.cleaned_data.get("playbook") or "",
            required_fields=form.cleaned_data.get("required_fields") or [],
            optional_fields=form.cleaned_data.get("optional_fields") or [],
            variable_points=form.cleaned_data["variable_points"],
            variable_cost_note=form.cleaned_data.get("variable_cost_note") or "",
            min_points=form.cleaned_data.get("min_points") or 0,
            max_points=form.cleaned_data.get("max_points") or 0,
            user_creatable=form.cleaned_data["user_creatable"],
            sort_order=form.cleaned_data.get("sort_order") or 0,
        )
        return redirect("admin_categories")

    return render(request, "admin/category_form.html", {})


@require_permission("gold_team", "ticketing_admin")
def admin_category_edit(request: HttpRequest, category_id: int) -> HttpResponse:
    from ticketing.models import TicketCategory

    try:
        category = TicketCategory.objects.get(pk=category_id)
    except TicketCategory.DoesNotExist:
        return HttpResponse("Category not found", status=404)

    if request.method == "POST":
        form = CategoryForm(request.POST)
        if not form.is_valid():
            error_msg = (
                "Display name is required."
                if "display_name" in form.errors
                else str(next(iter(form.errors.values()))[0])
            )
            return render(
                request,
                "admin/category_form.html",
                {
                    "category": category,
                    "error": error_msg,
                    "form_data": request.POST,
                },
            )

        category.display_name = form.cleaned_data["display_name"]
        category.points = form.cleaned_data["points"]
        category.playbook = form.cleaned_data.get("playbook") or ""
        category.required_fields = form.cleaned_data.get("required_fields") or []
        category.optional_fields = form.cleaned_data.get("optional_fields") or []
        category.variable_points = form.cleaned_data["variable_points"]
        category.variable_cost_note = form.cleaned_data.get("variable_cost_note") or ""
        category.min_points = form.cleaned_data.get("min_points") or 0
        category.max_points = form.cleaned_data.get("max_points") or 0
        category.user_creatable = form.cleaned_data["user_creatable"]
        category.sort_order = form.cleaned_data.get("sort_order") or 0
        category.save()
        return redirect("admin_categories")

    return render(request, "admin/category_form.html", {"category": category})


@require_permission("gold_team", "ticketing_admin")
def admin_category_delete(request: HttpRequest, category_id: int) -> HttpResponse:
    from ticketing.models import TicketCategory

    try:
        category = TicketCategory.objects.get(pk=category_id)
    except TicketCategory.DoesNotExist:
        return HttpResponse("Category not found", status=404)

    ticket_count = category.tickets.count()

    if request.method == "POST":
        category.delete()
        return redirect("admin_categories")

    return render(
        request,
        "admin/category_delete.html",
        {
            "category": category,
            "ticket_count": ticket_count,
        },
    )
