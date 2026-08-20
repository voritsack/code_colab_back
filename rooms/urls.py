from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import RoomViewSet

app_name = 'rooms'  # Добавляем имя приложения для работы namespace

router = DefaultRouter()
router.register(r'rooms', RoomViewSet, basename='room')

urlpatterns = [
    path('', include(router.urls)),
]