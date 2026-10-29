from .version import __version__

API_AUTH_HEADER = "Authorization"  # 阿里云鉴权头
API_VERSION_HEADER = "X-DashScope-Version"  # 阿里云版本头
API_VERSION = "2024-10-01"  # 阿里云API版本
MODEL_TYPE_HEADER = "X-DashScope-DataInspection"  # 数据检查头
USER_AGENT = f"LiveKit Agents Aliyun Plugin/{__version__}"  # 用户代理
REQUEST_ID_HEADER = "X-Ca-Request-Id"  # 请求ID头

# 阿里云API基础URL
BASE_URL_HTTP = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts"
BASE_URL_WEBSOCKET = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"

# 支持的语言
SUPPORTED_LANGUAGES = {
    "zh": "中文",
    "en": "英文",
    "ja": "日文",
    "ko": "韩文",
    "es": "西班牙文",
    "fr": "法文",
    "de": "德文",
    "it": "意大利文",
    "pt": "葡萄牙文",
    "ru": "俄文",
    "ar": "阿拉伯文",
    "th": "泰文",
    "vi": "越南文",
    "id": "印尼文",
    "ms": "马来文",
    "fil": "菲律宾文",
    "tr": "土耳其文",
    "hi": "印地文",
    "bn": "孟加拉文",
    "ur": "乌尔都文",
}

# 支持的音频格式
SUPPORTED_FORMATS = {
    "pcm": "pcm",
    "wav": "wav",
    "mp3": "mp3",
    "opus": "opus",
    "aac": "aac",
}

# 支持的采样率
SUPPORTED_SAMPLE_RATES = [8000, 16000, 22050, 24000, 44100, 48000]

# 支持的模型
SUPPORTED_MODELS = {
    "cosyvoice-v3-flash": "快速语音合成模型",
    "cosyvoice-v3": "标准语音合成模型",
    "cosyvoice-v2": "旧版语音合成模型",
    "sambert": "Sambert语音模型",
}

# 支持的发音人（部分常用发音人示例）
SUPPORTED_VOICES = {
    "zh": ["longanyang", "zhimiao", "zhitian", "zhiyan", "zhixia", "zhiyuan"],
    "en": ["katherine", "eric", "michael", "sarah", "lisa", "tom"],
    "ja": ["haruka", "takumi", "sayaka", "daichi"],
    "ko": ["jiwon", "minho", "sooyoung", "taehyun"],
}

# 错误码
ERROR_CODES = {
    400: "Bad Request - 请求参数错误",
    401: "Unauthorized - 鉴权失败",
    403: "Forbidden - 无权限访问",
    404: "Not Found - 资源不存在",
    429: "Too Many Requests - 请求频率超限",
    500: "Internal Server Error - 服务器内部错误",
    502: "Bad Gateway - 网关错误",
    503: "Service Unavailable - 服务不可用",
    504: "Gateway Timeout - 网关超时",
}

# 流式传输模式
STREAMING_MODES = {
    "duplex": "双向流式传输",
    "input": "仅输入流式",
    "output": "仅输出流式",
}

# 文本类型
TEXT_TYPES = {
    "PlainText": "纯文本",
    "SSML": "SSML标记语言",
}

# 音高范围
PITCH_RANGE = (0.5, 2.0)  # 音高调节范围

# 语速范围
RATE_RANGE = (0.5, 2.0)  # 语速调节范围

# 音量范围
VOLUME_RANGE = (0, 100)  # 音量调节范围

# 超时设置（单位：秒）
DEFAULT_TIMEOUT = 30
CONNECTION_TIMEOUT = 10
READ_TIMEOUT = 60
WRITE_TIMEOUT = 10