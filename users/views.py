from datetime import datetime, timedelta, timezone
import jwt
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from .models import CustomUser
from .serializers import UserSerializer
from .authentication import CookieJWTAuthentication

class RegisterView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = UserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        user = CustomUser.objects.filter(email=email).first()

        if user is None or not user.check_password(password):
            return Response(
                {'error': 'Invalid email or password'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        now = datetime.now(timezone.utc)
        payload = {
            'id': user.id,
            'exp': now + timedelta(hours=7),
            'iat': now,
        }

        token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')

        response = Response({'jwt': token, 'message': 'Success'})
        response.set_cookie(
            key='jwt',
            value=token,
            httponly=True,
            samesite='Lax',
            secure=not settings.DEBUG,
            path='/'
        )
        return response


class UserView(APIView):
    authentication_classes = [CookieJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            'id': user.id,
            'name': user.name,
            'email': user.email,
        })


class LogoutView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        response = Response({'message': 'Logged out successfully'})
        response.delete_cookie(
            key='jwt',
            path='/',
            samesite='Lax',
            httponly=True
        )
        return response