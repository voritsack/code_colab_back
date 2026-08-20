from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'^ws/room/(?P<room_id>\d+)/$', consumers.CodeSyncConsumer.as_asgi()),
    re_path(r'^ws/project/(?P<project_id>\d+)/$', consumers.CodeSyncConsumer.as_asgi()),
    re_path(r'^ws/file/(?P<file_id>\d+)/$', consumers.CodeSyncConsumer.as_asgi()),
]