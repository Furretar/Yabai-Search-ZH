from django.urls import path

from . import views

urlpatterns = [
    path("", views.index),
    path("about", views.about),
    path("word/<path:word>", views.word_page),
    path("reports", views.reports_page),
    path("reports/delete", views.delete_report),
    path("api/search", views.api_search),
    path("api/search-log", views.api_search_log),
    path("api/convert", views.api_convert),
    path("api/convert-batch", views.api_convert_batch),
    path("api/report", views.api_report),
    path("api/annotate", views.api_annotate),
    path("manifest.webmanifest", views.pwa_manifest),
    path("sw.js", views.pwa_service_worker),
    path("moe-glyphs/<str:name>", views.moe_glyph),
    path("__ping", views.debug_ping),
    path("__debug/status", views.debug_status),
]
