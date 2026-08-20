from rest_framework import viewsets, permissions
from .models import Room
from .serializers import RoomSerializer
from users.authentication import CookieJWTAuthentication


class RoomViewSet(viewsets.ModelViewSet):
    queryset = Room.objects.all()
    serializer_class = RoomSerializer
    authentication_classes = [CookieJWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)