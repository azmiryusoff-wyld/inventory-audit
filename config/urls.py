from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Inventory Audit"
admin.site.site_title = "Inventory Audit"
admin.site.index_title = "Master data"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("audit.urls")),
]
