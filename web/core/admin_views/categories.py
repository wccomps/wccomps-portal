from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from ticketing.forms import TicketCategoryForm

from ..auth_utils import require_permission


@require_permission("gold_team", "ticketing_admin")
def admin_categories(request: HttpRequest) -> HttpResponse:
    from ticketing.models import TicketCategory

    categories = TicketCategory.objects.all()
    return render(request, "admin/categories.html", {"categories": categories})


@require_permission("gold_team", "ticketing_admin")
def admin_category_create(request: HttpRequest) -> HttpResponse:

    if request.method == "POST":
        form = TicketCategoryForm(request.POST)
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

        form.save()
        return redirect("admin_categories")

    return render(request, "admin/category_form.html", {})


@require_permission("gold_team", "ticketing_admin")
def admin_category_edit(request: HttpRequest, category_id: int) -> HttpResponse:
    from ticketing.models import TicketCategory

    category = get_object_or_404(TicketCategory, pk=category_id)

    if request.method == "POST":
        form = TicketCategoryForm(request.POST, instance=category)
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

        form.save()
        return redirect("admin_categories")

    return render(request, "admin/category_form.html", {"category": category})


@require_permission("gold_team", "ticketing_admin")
def admin_category_delete(request: HttpRequest, category_id: int) -> HttpResponse:
    from ticketing.models import TicketCategory

    category = get_object_or_404(TicketCategory, pk=category_id)

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
