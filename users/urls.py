from django.urls import path
from . import views

app_name = 'users'

urlpatterns = [
    # Добавлены слэши в конце строк:
    path('register/', views.RegisterView.as_view(), name='register'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('user/', views.UserView.as_view(), name='user'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
]