from django.db import models
from django.conf import settings

class Room(models.Model):
    ROOM_TYPES = (
        ('team', 'Team'),
        ('edu', 'Education'),
        ('custom', 'Custom'),
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    type = models.CharField(max_length=10, choices=ROOM_TYPES, default='team')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='created_rooms')
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='joined_rooms', blank=True)
    max_users = models.PositiveIntegerField(default=10)
    is_private = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title