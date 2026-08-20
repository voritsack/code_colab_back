from rest_framework import serializers
from users.models import Post
from .models import Folder, CodeFile


class PostSerializer(serializers.ModelSerializer):
    author = serializers.ReadOnlyField(source='author.email')

    class Meta:
        model = Post
        fields = ['id', 'title', 'content', 'status', 'category', 'author']


class CodeFileSerializer(serializers.ModelSerializer):
    user = serializers.ReadOnlyField(source='user.username')
    folder = serializers.PrimaryKeyRelatedField(
        queryset=Folder.objects.all(), 
        required=False, 
        allow_null=True
    )
    path = serializers.CharField(source='get_relative_path', read_only=True)

    class Meta:
        model = CodeFile
        fields = '__all__'
        read_only_fields = ('user', 'version', 'created_at', 'updated_at', 'path')
        extra_kwargs = {
            'file_type': {'required': False, 'default': 'code'},
            'language': {'required': False, 'default': 'javascript'},
            'code_content': {'required': False, 'allow_blank': True, 'default': ''},
            'file': {'required': False, 'allow_null': True},
        }

    def create(self, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['user'] = request.user
        return super().create(validated_data)


class FolderSerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()
    files = CodeFileSerializer(many=True, read_only=True)

    class Meta:
        model = Folder
        fields = ['id', 'name', 'parent', 'children', 'files', 'created_at']

    def get_children(self, obj):
        serializer = FolderSerializer(obj.children.all(), many=True)
        return serializer.data


class SyncFileItemSerializer(serializers.Serializer):
    path = serializers.CharField(max_length=500)
    content = serializers.CharField(allow_blank=True, required=False, allow_null=True)
    code = serializers.CharField(allow_blank=True, required=False, allow_null=True)


class ProjectSyncSerializer(serializers.Serializer):
    project_name = serializers.CharField(max_length=255)
    files = SyncFileItemSerializer(many=True)