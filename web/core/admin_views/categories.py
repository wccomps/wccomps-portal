from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from ticketing.forms import TicketCategoryForm

from ..auth_utils import require_permission
from ..utils import list_filters, requested_sort, stable_order


@require_permission("gold_team", "ticketing_admin")
def admin_categories(request: HttpRequest) -> HttpResponse:
    from ticketing.models import TicketCategory

    filters, filtered = list_filters(request, {"visibility": "", "search": ""})
    categories = TicketCategory.objects.annotate(ticket_count=Count("tickets"))

    if filters["visibility"] == "visible":
        categories = categories.filter(user_creatable=True)
    elif filters["visibility"] == "hidden":
        categories = categories.filter(user_creatable=False)

    if search := filters["search"]:
        categories = categories.filter(Q(display_name__icontains=search) | Q(playbook__icontains=search))

    sort_by = requested_sort(
        request,
        ("sort_order", "display_name", "points", "ticket_count"),
        "sort_order",
    )
    categories = stable_order(categories, sort_by)

    context = {
        "categories": categories,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
    }

    if request.headers.get("HX-Request"):
        return render(request, "admin/categories_table.html", context)

    return render(request, "admin/categories.html", context)


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
