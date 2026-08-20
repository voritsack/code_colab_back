import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from users.models import CustomUser 

class CookieJWTAuthentication(BaseAuthentication):
    def authenticate(self, request):
        auth_header = request.headers.get('Authorization')
        token = None

        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        else:
            token = request.COOKIES.get('jwt')

        if not token:
            return None  

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed('Срок действия токена истек')
        except jwt.InvalidTokenError:
            raise AuthenticationFailed('Невалидный токен')

        user_id = payload.get('id') or payload.get('user_id')
        if not user_id:
            raise AuthenticationFailed('Неверный формат токена')

        user = CustomUser.objects.filter(id=user_id).first()
        if not user:
            raise AuthenticationFailed('Пользователь не найден')

        return (user, None)