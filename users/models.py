from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils import timezone
from django.conf import settings  # <--- ДОБАВЛЕН ИМПОРТ SETTINGS


# Менеджер для CustomUser (нужен, так как убрали username)
class CustomUserManager(BaseUserManager):
    def create_user(self, email, name, password=None, **extra_fields):
        if not email:
            raise ValueError('Email обязателен')
        
        email = self.normalize_email(email)
        
        # Гарантируем, что создаваемый пользователь активен
        extra_fields.setdefault('is_active', True)
        
        user = self.model(email=email, name=name, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, name, password=None, **extra_fields):
        # Настройки для Суперпользователя
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Суперпользователь должен иметь is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Суперпользователь должен иметь is_superuser=True.')

        return self.create_user(email, name, password, **extra_fields)
    
class CustomUser(AbstractUser):
    username = None  # Удаляем username
    name = models.CharField(max_length=255)
    email = models.EmailField(max_length=255, unique=True)

    USERNAME_FIELD = 'email'  # Авторизация по email
    REQUIRED_FIELDS = ['name']  # Поля при создании superuser

    objects = CustomUserManager()  # Привязываем кастомный менеджер


class Category(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Post(models.Model):

    class PostObjects(models.Manager):
        def get_queryset(self):
            # Авто-фильтрация: вернет только опубликованные посты
            return super().get_queryset().filter(status='published')

    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('published', 'Published'),
    )

    title = models.CharField(max_length=250)
    content = models.TextField()
    excerpt = models.TextField(null=True, blank=True)
    
    # Категория (оставлена одна запись)
    category = models.ForeignKey(
        Category, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        default=1,
    )
    
    # Автор
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='blog_posts'
    )
    
    published = models.DateTimeField(default=timezone.now)
    slug = models.SlugField(max_length=250, unique_for_date='published')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='published')
    
    # Менеджеры
    objects = models.Manager()  # Post.objects.all() - вернет ВСЕ посты
    postobjects = PostObjects()  # Post.postobjects.all() - вернет ТОЛЬКО published

    # Вариант 1: Поле ТОЛЬКО для картинок (с проверкой формата)
    image = models.ImageField(
        upload_to='blog_images/%Y/%m/%d/', 
        null=True, 
        blank=True,
        verbose_name='Изображение'
    )

    # Вариант 2: Поле для ЛЮБЫХ файлов (pdf, zip, docx и т.д.)
    file = models.FileField(
        upload_to='blog_files/%Y/%m/%d/', 
        null=True, 
        blank=True,
        verbose_name='Прикрепленный файл'
    )

    class Meta:
        ordering = ('-published',)

    def __str__(self):
        return self.title


