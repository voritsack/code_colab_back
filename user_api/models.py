from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError

# Кастомный валидатор размера файла (5 МБ)
def validate_file_size(value):
    max_size_mb = 5
    if value.size > max_size_mb * 1024 * 1024:
        raise ValidationError(f'Максимальный размер файла — {max_size_mb} МБ')


class Folder(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='folders')
    name = models.CharField(max_length=255)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class CodeFile(models.Model):
    TYPE_CHOICES = (
        ('code', 'Код (Текст)'),
        ('file', 'Файл'),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='code_files')
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, null=True, blank=True, related_name='files')
    title = models.CharField(max_length=255)
    file_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='code')
    
    code_content = models.TextField(blank=True, default='')
    language = models.CharField(max_length=50, default='javascript', blank=True)
    version = models.PositiveIntegerField(default=0)

    # Применяем написанный выше валидатор размера   
    file = models.FileField(
        upload_to='user_codes/', 
        null=True, 
        blank=True,
        validators=[validate_file_size]
    )
    
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    def get_relative_path(self):
        """Возвращает относительный путь относительно корня проекта.
        Например: 'components/UI/Button.jsx' или 'main.py'
        """
        path_parts = [self.title]
        current_folder = self.folder
    
        # Идем вверх до тех пор, пока у папки есть родитель (т.е. пока не упремся в root_folder проекта)
        while current_folder and current_folder.parent is not None:
            path_parts.append(current_folder.name)
            current_folder = current_folder.parent
    
        path_parts.reverse()
        return "/".join(path_parts)