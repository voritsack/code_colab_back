# BACK/users/admin.py (или BACK/apiP/admin.py)

from django.contrib import admin
from .models import CodeFile, Folder  # Укажите имя вашей модели файлов

@admin.register(CodeFile)
class CodeFileAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'user', 'created_at') # Поля для отображения в таблице
    search_fields = ('title', 'user__username')

@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'user')