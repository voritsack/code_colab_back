from django.urls import path
from . import views

app_name = 'user_api'

urlpatterns = [
    path('posts/', views.PostListView.as_view(), name='post_list'),
    path('posts/<int:pk>/', views.PostDetailView.as_view(), name='post_detail'),
    
    # Файлы
    path('files/', views.CodeFileViewSet.as_view({'get': 'list', 'post': 'create'}), name='file_list'),
    path('files/<int:pk>/', views.CodeFileDetailView.as_view(), name='file_detail'),
    path('my-files/', views.MyCodeFilesListView.as_view(), name='my-files'),
    
    # Папки и Проекты
    path('folders/', views.FolderListCreateView.as_view(), name='folder_list'),
    path('projects/<int:project_id>/save/', views.SaveProjectStateView.as_view(), name='save_project'),
    path('items/delete/', views.DeleteItemView.as_view(), name='delete_item'),
    
    # Синхронизация VS Code
    path('projects/sync/', views.ProjectSyncView.as_view(), name='project-sync'),
]