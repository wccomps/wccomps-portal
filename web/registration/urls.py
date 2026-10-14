from django.urls import path

from . import views

urlpatterns = [
    # Public registration
    path("", views.register, name="registration_register"),
    path("edit/<str:token>/", views.registration_edit, name="registration_edit"),
    # Admin review
    path("review/", views.review_list, name="registration_review_list"),
    path("approve/<int:registration_id>/", views.approve_registration, name="registration_approve"),
    path("reject/<int:registration_id>/", views.reject_registration, name="registration_reject"),
    path("seasons/", views.season_list, name="registration_season_list"),
    path("seasons/create/", views.season_create, name="registration_season_create"),
    path("seasons/<int:season_id>/edit/", views.season_edit, name="registration_season_edit"),
    path("seasons/<int:season_id>/delete/", views.season_delete, name="registration_season_delete"),
    path("seasons/<int:season_id>/events/", views.event_list, name="registration_event_list"),
    path("seasons/<int:season_id>/events/create/", views.event_create, name="registration_event_create"),
    path("events/<int:event_id>/", views.event_detail, name="registration_event_detail"),
    path("events/<int:event_id>/edit/", views.event_edit, name="registration_event_edit"),
    path("events/<int:event_id>/delete/", views.event_delete, name="registration_event_delete"),
    path("events/<int:event_id>/assign-teams/", views.assign_teams, name="registration_assign_teams"),
    path("assignments/<int:assignment_id>/unassign/", views.unassign_team, name="registration_unassign_team"),
]
