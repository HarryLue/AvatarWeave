"""Local Kokoro streaming TTS plugin for LiveKit Agents."""

import logging

from livekit.agents import Plugin

from .tts import TTS

__version__ = "0.1.0"
__all__ = ["TTS"]


class KokoroPlugin(Plugin):
    def __init__(self):
        super().__init__(
            __name__, __version__, __package__, logging.getLogger(__name__)
        )


Plugin.register_plugin(KokoroPlugin())
