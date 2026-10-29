"""Curated Mandarin/English voices; metadata from the upstream Kokoro-82M voice card."""

SOURCE = "https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md"
# Grades describe the upstream training-data estimate, not a listening score.
ROWS = [
    ("zf_xiaobei", "小北", "zh", "女声", "D"),
    ("zf_xiaoni", "小妮", "zh", "女声", "D"),
    ("zf_xiaoxiao", "小小", "zh", "女声", "D"),
    ("zf_xiaoyi", "小伊", "zh", "女声", "D"),
    ("zm_yunjian", "云健", "zh", "男声", "D"),
    ("zm_yunxi", "云希", "zh", "男声", "D"),
    ("zm_yunxia", "云夏", "zh", "男声", "D"),
    ("zm_yunyang", "云扬", "zh", "男声", "D"),
    ("af_heart", "Heart", "en-us", "女声", "A"),
    ("af_alloy", "Alloy", "en-us", "女声", "C"),
    ("af_aoede", "Aoede", "en-us", "女声", "C+"),
    ("af_bella", "Bella", "en-us", "女声", "A-"),
    ("af_jessica", "Jessica", "en-us", "女声", "D"),
    ("af_kore", "Kore", "en-us", "女声", "C+"),
    ("af_nicole", "Nicole", "en-us", "女声", "B-"),
    ("af_nova", "Nova", "en-us", "女声", "C"),
    ("af_river", "River", "en-us", "女声", "D"),
    ("af_sarah", "Sarah", "en-us", "女声", "C+"),
    ("af_sky", "Sky", "en-us", "女声", "C-"),
    ("am_adam", "Adam", "en-us", "男声", "F+"),
    ("am_echo", "Echo", "en-us", "男声", "D"),
    ("am_eric", "Eric", "en-us", "男声", "D"),
    ("am_fenrir", "Fenrir", "en-us", "男声", "C+"),
    ("am_liam", "Liam", "en-us", "男声", "D"),
    ("am_michael", "Michael", "en-us", "男声", "C+"),
    ("am_onyx", "Onyx", "en-us", "男声", "D"),
    ("am_puck", "Puck", "en-us", "男声", "C+"),
    ("am_santa", "Santa", "en-us", "男声", "D-"),
    ("bf_alice", "Alice", "en-gb", "女声", "D"),
    ("bf_emma", "Emma", "en-gb", "女声", "B-"),
    ("bf_isabella", "Isabella", "en-gb", "女声", "C"),
    ("bf_lily", "Lily", "en-gb", "女声", "D"),
    ("bm_daniel", "Daniel", "en-gb", "男声", "D"),
    ("bm_fable", "Fable", "en-gb", "男声", "C"),
    ("bm_george", "George", "en-gb", "男声", "C"),
    ("bm_lewis", "Lewis", "en-gb", "男声", "D+"),
]
LANGUAGES = {"zh": "普通话", "en-us": "美式英语", "en-gb": "英式英语"}
VOICES = {
    id: {
        "id": id,
        "name": name,
        "language": language,
        "language_label": LANGUAGES[language],
        "gender": gender,
        "grade": grade,
        "description": f"{LANGUAGES[language]}{gender} · {name}。"
        + (
            "中文日常对话与讲解试听候选；中文训练样本较少，建议先试听。"
            if language == "zh"
            else f"适合英语对话、朗读试听；官方训练数据等级 {grade}。"
        ),
        "preview_text": "你好，我是你的数字人助手。很高兴和你一起交流。"
        if language == "zh"
        else "Hello, I am your digital assistant. It is a pleasure to speak with you.",
    }
    for id, name, language, gender, grade in ROWS
}


def get_voice(voice_id: str) -> dict:
    if voice_id not in VOICES:
        raise ValueError("Unsupported Kokoro voice")
    return VOICES[voice_id]

NOTICE = "中文音色训练样本较少，建议试听后选择；英语音色适合英语交流。"
