import json
from channels.generic.websocket import AsyncWebsocketConsumer

class CodeSyncConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        kwargs = self.scope['url_route']['kwargs']

        if 'room_id' in kwargs:
            self.group_name = f"room_{kwargs['room_id']}"
        elif 'project_id' in kwargs:
            self.group_name = f"project_{kwargs['project_id']}"
        elif 'file_id' in kwargs:
            self.group_name = f"file_{kwargs['file_id']}"
        else:
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "broadcast_message",
                "message": data,
                "sender_channel_name": self.channel_name
            }
        )

    async def broadcast_message(self, event):
        if self.channel_name == event.get("sender_channel_name"):
            return

        await self.send(text_data=json.dumps(event["message"]))