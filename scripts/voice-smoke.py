"""Real model/room latency check. --exercise-asr also sends synthesized speech as microphone audio."""

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from avatarweave_agent.config import settings
from avatarweave_agent.pipeline import build_provider, load_pipeline
from livekit import rtc


def fetch_credentials():
    headers = {"Content-Type": "application/json"}
    if settings.api_access_key:
        headers["Authorization"] = "Bearer " + settings.api_access_key
    request = urllib.request.Request(
        "http://127.0.0.1:8090/api/token", data=b"{}", headers=headers
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


async def main(exercise_asr=False):
    credentials = await asyncio.to_thread(fetch_credentials)
    room = rtc.Room()
    audio, text = asyncio.Event(), asyncio.Event()
    tasks = set()
    current = {"audio_at": None, "text_chars": 0, "asr_chars": 0}
    result = {}

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)

        def complete(t):
            tasks.discard(t)
            if not t.cancelled() and t.exception():
                print(
                    f"Consumer failed: {type(t.exception()).__name__}", file=sys.stderr
                )

        task.add_done_callback(complete)

    async def consume_text(reader, identity):
        message = await reader.read_all()
        if identity == room.local_participant.identity:
            current["asr_chars"] += len(message)
        else:
            current["text_chars"] += len(message)
            text.set()

    room.register_text_stream_handler(
        "lk.transcription",
        lambda reader, identity: spawn(consume_text(reader, identity)),
    )

    async def consume_audio(track):
        frames = rtc.AudioStream(track)
        try:
            async for event in frames:
                if (
                    max(map(abs, event.frame.data), default=0) > 500
                    and current["audio_at"] is None
                ):
                    current["audio_at"] = time.perf_counter()
                    audio.set()
        finally:
            await frames.aclose()

    @room.on("track_subscribed")
    def on_track(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            spawn(consume_audio(track))

    async def settled():
        await asyncio.wait_for(text.wait(), 30)
        for _ in range(300):
            if any(
                p.attributes.get("lk.agent.state") == "listening"
                for p in room.remote_participants.values()
            ):
                await asyncio.sleep(0.3)
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("Agent did not finish speaking")

    def reset():
        current.update(audio_at=None, text_chars=0, asr_chars=0)
        audio.clear()
        text.clear()

    try:
        started = time.perf_counter()
        await room.connect(credentials["server_url"], credentials["participant_token"])
        await asyncio.wait_for(audio.wait(), 90)
        result["connect_to_greeting_audio_ms"] = round(
            (current["audio_at"] - started) * 1000
        )
        await settled()
        reset()
        started = time.perf_counter()
        await room.local_participant.send_text("请只回复一句你好。", topic="lk.chat")
        await asyncio.wait_for(audio.wait(), 60)
        result["text_to_reply_audio_ms"] = round((current["audio_at"] - started) * 1000)
        await settled()
        if exercise_asr:
            config = load_pipeline()
            tts = build_provider("tts", config.tts)
            frames = []
            async with tts.synthesize("你好，请用一句话介绍一下自己。") as synthesis:
                async for output in synthesis:
                    frames.append(output.frame)
            if not frames:
                raise RuntimeError("Input speech synthesis produced no audio")
            source = rtc.AudioSource(frames[0].sample_rate, frames[0].num_channels)
            track = rtc.LocalAudioTrack.create_audio_track("voice-smoke-input", source)
            await room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )
            reset()
            for frame in frames:
                await source.capture_frame(frame)
            await source.wait_for_playout()
            speech_end = time.perf_counter()
            samples = source.sample_rate // 50
            for _ in range(35):
                await source.capture_frame(
                    rtc.AudioFrame(
                        data=bytes(samples * 2 * source.num_channels),
                        sample_rate=source.sample_rate,
                        num_channels=source.num_channels,
                        samples_per_channel=samples,
                    )
                )
            await asyncio.wait_for(audio.wait(), 60)
            result["speech_end_to_reply_audio_ms"] = round(
                (current["audio_at"] - speech_end) * 1000
            )
            await settled()
            result["asr_transcript_chars"] = current["asr_chars"]
            await source.aclose()
            await tts.aclose()
            if not current["asr_chars"]:
                raise AssertionError("No microphone transcription received")
        print(json.dumps({"status": "passed", **result}, ensure_ascii=False))
    finally:
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await room.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exercise-asr", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.exercise_asr))
