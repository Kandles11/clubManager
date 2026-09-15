from django.urls import path
from . import views

urlpatterns = [
    path("register", views.RegistrationRequestView.as_view(), name="registration_request"),
    path(
        "register/continue/<uuid:user_registration_key>",
        views.RegistrationCompleteView.as_view(),
        name="registration_complete",
    ),
    path("link/<uuid:uuid>", views.LinkSocialView.as_view(), name="link_social"),
    path("link_success", views.LinkSuccessView.as_view(), name="link_success"),
]
