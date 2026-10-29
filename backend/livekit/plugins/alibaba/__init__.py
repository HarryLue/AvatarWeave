# livekit/plugins/alibaba/__init__.py

from .stt import STT
from .tts import TTS
from .version import __version__
from .log import logger

from livekit.agents import Plugin

__all__ = ["STT", "TTS", "__version__"]

class AlibabaPlugin(Plugin):  # ✅ 改为 AlibabaPlugin
    def __init__(self) -> None:
        super().__init__(__name__, __version__, __package__, logger)

# 注册插件
Plugin.register_plugin(AlibabaPlugin())

# 隐藏非公开成员（可选）
_module = dir()
NOT_IN_ALL = [m for m in _module if m not in __all__]
__pdoc__ = {n: False for n in NOT_IN_ALL}