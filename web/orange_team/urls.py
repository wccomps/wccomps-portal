from django.urls import path

from orange_team import views

app_name = "orange_team"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("check-in/", views.toggle_checkin, name="toggle_checkin"),
    path("check-in/<int:user_id>/", views.admin_toggle_checkin, name="admin_toggle_checkin"),
    path("review/", views.review_queue, name="review_queue"),
    path("checks/", views.check_list, name="check_list"),
    path("checks/auto-assign/", views.auto_assign_all, name="auto_assign_all"),
    path("checks/create/", views.check_create, name="check_create"),
    path("checks/<int:check_id>/", views.check_detail, name="check_detail"),
    path("checks/<int:check_id>/edit/", views.check_edit, name="check_edit"),
    path("checks/<int:check_id>/duplicate/", views.check_duplicate, name="check_duplicate"),
    path("checks/<int:check_id>/assign/", views.check_assign, name="check_assign"),
    path("checks/<int:check_id>/auto-assign/", views.check_auto_assign, name="check_auto_assign"),
    path("checks/<int:check_id>/rebalance/", views.check_rebalance, name="check_rebalance"),
    path("assignments/<int:assignment_id>/reassign/", views.reassign_team, name="reassign_team"),
    path("assignments/<int:assignment_id>/save/", views.assignment_save, name="assignment_save"),
    path("assignments/<int:assignment_id>/submit/", views.assignment_submit, name="assignment_submit"),
    path("assignments/<int:assignment_id>/approve/", views.assignment_approve, name="assignment_approve"),
    path("assignments/<int:assignment_id>/reject/", views.assignment_reject, name="assignment_reject"),
    path("export/", views.export_scores, name="export_scores"),
    path("followups/create/", views.followup_create, name="followup_create"),
    path("followups/<int:followup_id>/dismiss/", views.followup_dismiss, name="followup_dismiss"),
]
