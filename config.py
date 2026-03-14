import os
from pathlib import Path
from dotenv import load_dotenv

# 从项目根目录加载 .env
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

DATA_DIR = Path(os.getenv("DOUYIN_DATA_DIR", str(Path(__file__).resolve().parent / "douyin_data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TEMP_DIR = DATA_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_DIR = DATA_DIR / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_DIR = DATA_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
