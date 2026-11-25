from django.contrib import admin
from django.urls import include, path

from audit.login_guard import LoginForm

admin.site.site_header = "Inventory Audit"
admin.site.site_title = "Inventory Audit"
admin.site.index_title = "Admin"
admin.site.login_form = LoginForm

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("audit.urls")),
]
