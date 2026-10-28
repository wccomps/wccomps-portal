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
    form = TicketCategoryForm(request.POST if request.method == "POST" else None)
    if form.is_valid():
        form.save()
        return redirect("admin_categories")
    return render(request, "admin/category_form.html", {"form": form})


@require_permission("gold_team", "ticketing_admin")
def admin_category_edit(request: HttpRequest, category_id: int) -> HttpResponse:
    from ticketing.models import TicketCategory

    category = get_object_or_404(TicketCategory, pk=category_id)
    form = TicketCategoryForm(request.POST if request.method == "POST" else None, instance=category)
    if form.is_valid():
        form.save()
        return redirect("admin_categories")
    return render(request, "admin/category_form.html", {"category": category, "form": form})


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
