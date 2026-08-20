from rest_framework import serializers
from .models import Room


class RoomSerializer(serializers.ModelSerializer):
    current_users = serializers.IntegerField(source='members.count', read_only=True)
    owner_name = serializers.CharField(source='owner.username', read_only=True)
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = Room
        fields = [
            'id', 'title', 'description', 'type', 
            'max_users', 'is_private', 'current_users', 
            'owner_name', 'is_owner'
        ]
        read_only_fields = ['owner']

    def get_is_owner(self, obj):
        request = self.context.get('request')
        if request and request.user and request.user.is_authenticated:
            return obj.owner == request.user
        return False