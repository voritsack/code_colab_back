from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.authentication import TokenAuthentication, SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

from users.models import Post
from users.authentication import CookieJWTAuthentication
from .models import Folder, CodeFile
from .serializers import (
    PostSerializer, 
    FolderSerializer, 
    CodeFileSerializer, 
    ProjectSyncSerializer
)

DEFAULT_AUTH_CLASSES = [CookieJWTAuthentication, JWTAuthentication, TokenAuthentication, SessionAuthentication]


class CodeFileViewSet(viewsets.ModelViewSet):
    serializer_class = CodeFileSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return CodeFile.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        title = request.data.get('title')
        existing_file = CodeFile.objects.filter(
            user=request.user, 
            title=title, 
            folder__isnull=True
        ).first()
        
        if existing_file:
            serializer = self.get_serializer(existing_file, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return super().create(request, *args, **kwargs)


@method_decorator(csrf_exempt, name='dispatch')
class SaveProjectStateView(APIView):
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, project_id):
        files_data = request.data.get('files', [])

        for item in files_data:
            file_id = item.get('id')
            file_path = item.get('path')
            # Безопасно проверяем и 'code', и 'content'
            content = item.get('code') or item.get('content', '')

            if file_id:
                CodeFile.objects.filter(id=file_id, user=request.user).update(code_content=content)
            elif file_path:
                file_name = file_path.split('/')[-1]
                CodeFile.objects.filter(
                    title=file_name,
                    user=request.user,
                    folder_id=project_id
                ).update(code_content=content)

        return Response({'status': 'project saved'}, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class DeleteItemView(APIView):
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        item_type = request.data.get('type')
        item_id = request.data.get('id')

        if item_type == 'file':
            CodeFile.objects.filter(id=item_id, user=request.user).delete()
        elif item_type == 'folder':
            Folder.objects.filter(id=item_id, user=request.user).delete()
        else:
            return Response({'error': 'Invalid type'}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'status': 'deleted'}, status=status.HTTP_200_OK)


class ProjectSyncView(APIView):
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        serializer = ProjectSyncSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        project_name = serializer.validated_data['project_name']
        file_paths = serializer.validated_data['files']
        user = request.user

        root_folder, _ = Folder.objects.get_or_create(
            name=project_name,
            user=user,
            parent=None
        )

        folder_cache = {(): root_folder}

        for item in file_paths:
            full_path = item.get('path', '') if isinstance(item, dict) else item
            # Безопасно получаем контент с учетом обоих вариантов ключей
            content = (item.get('code') or item.get('content', '')) if isinstance(item, dict) else ''

            parts = [p for p in full_path.replace('\\', '/').split('/') if p]
            if not parts:
                continue

            current_folder = root_folder
            path_accumulator = []
            
            for folder_name in parts[:-1]:
                path_accumulator.append(folder_name)
                path_key = tuple(path_accumulator)

                if path_key not in folder_cache:
                    folder_obj, _ = Folder.objects.get_or_create(
                        name=folder_name,
                        user=user,
                        parent=current_folder
                    )
                    folder_cache[path_key] = folder_obj
                
                current_folder = folder_cache[path_key]

            file_name = parts[-1]
            CodeFile.objects.update_or_create(
                title=file_name,
                user=user,
                folder=current_folder,
                defaults={'code_content': content}
            )

        result_serializer = FolderSerializer(root_folder)
        return Response({
            'project_id': root_folder.id,
            'id': root_folder.id,
            'tree': result_serializer.data
        }, status=status.HTTP_201_CREATED)


class FolderListCreateView(generics.ListCreateAPIView):
    serializer_class = FolderSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Folder.objects.filter(user=self.request.user, parent__isnull=True)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class CodeFileDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CodeFileSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return CodeFile.objects.filter(user=self.request.user)


class MyCodeFilesListView(generics.ListAPIView):
    serializer_class = CodeFileSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return CodeFile.objects.filter(user=self.request.user)


class CodeFileCreateView(generics.CreateAPIView):
    serializer_class = CodeFileSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class PostListView(generics.ListCreateAPIView):
    queryset = Post.objects.all()
    serializer_class = PostSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)


class PostDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Post.objects.all()
    serializer_class = PostSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]


class PostViewSet(viewsets.ModelViewSet):
    queryset = Post.objects.all()
    serializer_class = PostSerializer
    authentication_classes = DEFAULT_AUTH_CLASSES
    permission_classes = [permissions.IsAuthenticated]