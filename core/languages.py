"""
core/languages.py

Master list of supported languages.
Any language added to language_configs must exist in this list.
"""

SUPPORTED_LANGUAGES: list[dict] = [
    {"code": "tai-lo",     "label_zh": "台語（台羅拼音）", "label_en": "Taiwanese (Tâi-lô)",    "default_direction": "source"},
    {"code": "hakka",      "label_zh": "客語",            "label_en": "Hakka",                  "default_direction": "source"},
    {"code": "indigenous", "label_zh": "原住民族語",       "label_en": "Indigenous Languages",   "default_direction": "source"},
    {"code": "zh-tw",      "label_zh": "繁體中文",         "label_en": "Traditional Chinese",     "default_direction": "both"},
    {"code": "zh-cn",      "label_zh": "簡體中文",         "label_en": "Simplified Chinese",      "default_direction": "both"},
    {"code": "en",         "label_zh": "英語",             "label_en": "English",                 "default_direction": "target"},
    {"code": "ja",         "label_zh": "日語",             "label_en": "Japanese",                "default_direction": "target"},
    {"code": "ko",         "label_zh": "韓語",             "label_en": "Korean",                  "default_direction": "target"},
    {"code": "fr",         "label_zh": "法語",             "label_en": "French",                  "default_direction": "target"},
    {"code": "de",         "label_zh": "德語",             "label_en": "German",                  "default_direction": "target"},
    {"code": "es",         "label_zh": "西班牙語",          "label_en": "Spanish",                 "default_direction": "target"},
    {"code": "vi",         "label_zh": "越南語",            "label_en": "Vietnamese",              "default_direction": "target"},
    {"code": "th",         "label_zh": "泰語",             "label_en": "Thai",                    "default_direction": "target"},
    {"code": "cs",         "label_zh": "捷克語",           "label_en": "Czech",                   "default_direction": "target"},
]

SUPPORTED_CODES = {lang["code"] for lang in SUPPORTED_LANGUAGES}
