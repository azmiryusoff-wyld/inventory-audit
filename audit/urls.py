from django.urls import path

from . import data_views as data
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("close/", views.month_close, name="month_close"),
    path("help/", views.help_page, name="help"),
    path("audit/<int:pk>/", views.audit_detail, name="audit_detail"),
    path("audit/<int:pk>/variance/", views.audit_detail, {"tab": "variance"}, name="audit_variance"),
    path("audit/<int:pk>/menu/", views.audit_detail, {"tab": "menu"}, name="audit_menu"),
    path("audit/<int:pk>/consumption/", views.audit_detail, {"tab": "consumption"}, name="audit_consumption"),
    path("audit/<int:pk>/inputs/", views.audit_detail, {"tab": "inputs"}, name="audit_inputs"),
    path("audit/<int:pk>/history/", views.audit_detail, {"tab": "history"}, name="audit_history"),
    path("audit/<int:pk>/lines/<str:kind>/new/", views.line_edit, name="audit_line_new"),
    path("audit/<int:pk>/lines/<str:kind>/<int:line_pk>/", views.line_edit, name="audit_line_edit"),
    path("audit/<int:pk>/upload/<str:kind>/", views.upload, name="audit_upload"),
    path("audit/<int:pk>/sign-off/", views.sign_off, name="audit_sign_off"),
    path("audit/<int:pk>/copy-opening/", views.copy_opening, name="audit_copy_opening"),
    path("audit/<int:pk>/map-pos-name/", views.map_pos_name, name="audit_map_pos_name"),
    path("templates/<str:kind>.csv", views.upload_template, name="upload_template"),
    path("audit/<int:pk>/export.xlsx", views.export_xlsx, name="audit_export"),
    path("brand/<str:brand_code>/menu-costing/", views.menu_costing, name="menu_costing"),
    path("trends/", views.trends, name="trends"),
    # Menus, recipes and the item master
    path("data/", data.data_home, name="data_home"),
    path("data/items/", data.item_list, name="data_items"),
    path("data/items/<str:sku>/", data.item_detail, name="data_item"),
    path("data/substitutions/", data.sub_list, name="data_subs"),
    path("data/substitutions/new/", data.sub_edit, name="data_sub_new"),
    path("data/substitutions/<int:pk>/", data.sub_edit, name="data_sub_edit"),
    path("data/changes/", data.change_log, name="data_changes"),
    path("data/<str:brand_code>/menu/", data.menu_list, name="data_menu"),
    path("data/<str:brand_code>/menu/new/", data.menu_edit, name="data_menu_new"),
    path("data/<str:brand_code>/menu/<int:pk>/", data.menu_edit, name="data_menu_edit"),
    path("data/<str:brand_code>/semi-finished/", data.sf_list, name="data_sf"),
    path("data/<str:brand_code>/semi-finished/new/", data.sf_edit, name="data_sf_new"),
    path("data/<str:brand_code>/semi-finished/<int:pk>/", data.sf_edit, name="data_sf_edit"),
    path("data/<str:brand_code>/recipes/", data.recipe_list, name="data_recipes"),
    path("data/<str:brand_code>/recipes/new/", data.recipe_edit, name="data_recipe_new"),
    path("data/<str:brand_code>/recipes/<int:pk>/", data.recipe_edit, name="data_recipe_edit"),
    path("data/<str:brand_code>/recipes/<int:pk>/delete/", data.recipe_delete, name="data_recipe_delete"),
]
