"""Run against the local stack: backend/.venv/bin/python scripts/smoke.py"""
import asyncio
import json
import urllib.request
from livekit import rtc

async def main():
    request = urllib.request.Request('http://127.0.0.1:8090/api/token', data=b'{}', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request) as response:
        credentials = json.load(response)
    room = rtc.Room()
    received = asyncio.Event()
    command = asyncio.Event()
    chunks = []
    tasks = set()
    async def consume(reader, identity):
        chunks.append(await reader.read_all())
        received.set()
    def on_text(reader, identity):
        task = asyncio.create_task(consume(reader, identity))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    room.register_text_stream_handler('lk.transcription', on_text)
    @room.on('data_received')
    def on_data(packet):
        if packet.topic == 'avatarweave.command.v1':
            payload = json.loads(packet.data)
            assert payload['version'] == 1 and payload['type'] == 'motion'
            command.set()
    try:
        await room.connect(credentials['server_url'], credentials['participant_token'])
        for _ in range(150):
            if any(p.attributes.get('lk.agent.state') == 'listening' for p in room.remote_participants.values()):
                break
            await asyncio.sleep(.2)
        else:
            raise AssertionError('Agent did not become ready')
        await room.local_participant.send_text('你好，请做一个动作', topic='lk.chat')
        await asyncio.wait_for(received.wait(), 20)
        await asyncio.wait_for(command.wait(), 5)
        assert '你好，请做一个动作' in chunks[0]
        print(json.dumps({'room': credentials['room_name'], 'reply': chunks[0], 'command': command.is_set()}, ensure_ascii=False))
    finally:
        await room.disconnect()

asyncio.run(main())
