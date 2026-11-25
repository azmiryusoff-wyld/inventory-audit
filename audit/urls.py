from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("audit/<int:pk>/", views.audit_detail, name="audit_detail"),
    path("audit/<int:pk>/variance/", views.audit_detail, {"tab": "variance"}, name="audit_variance"),
    path("audit/<int:pk>/menu/", views.audit_detail, {"tab": "menu"}, name="audit_menu"),
    path("audit/<int:pk>/consumption/", views.audit_detail, {"tab": "consumption"}, name="audit_consumption"),
    path("audit/<int:pk>/inputs/", views.audit_detail, {"tab": "inputs"}, name="audit_inputs"),
    path("audit/<int:pk>/upload/<str:kind>/", views.upload, name="audit_upload"),
    path("audit/<int:pk>/sign-off/", views.sign_off, name="audit_sign_off"),
    path("audit/<int:pk>/copy-opening/", views.copy_opening, name="audit_copy_opening"),
    path("audit/<int:pk>/map-pos-name/", views.map_pos_name, name="audit_map_pos_name"),
    path("templates/<str:kind>.csv", views.upload_template, name="upload_template"),
    path("audit/<int:pk>/export.xlsx", views.export_xlsx, name="audit_export"),
    path("brand/<str:brand_code>/menu-costing/", views.menu_costing, name="menu_costing"),
    path("trends/", views.trends, name="trends"),
]
