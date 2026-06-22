from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_user_id: int = 0

    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    # voice delivery knobs (lower stability + higher style = more energy)
    voice_stability: float = 0.35
    voice_style: float = 0.45
    voice_similarity: float = 0.75
    voice_speed: float = 1.0

    zernio_api_key: str = ""
    zernio_base_url: str = "https://zernio.com/api/v1"
    zernio_x_account_id: str = ""
    zernio_linkedin_account_id: str = ""

    claude_code_command: str = "claude"
    claude_code_extra_args: str = ""
    claude_timeout_s: int = 600

    drive_remote: str = "gdrive:VideoJournal"

    trip_start_date: date = date(2026, 6, 15)
    timezone: str = "America/Los_Angeles"
    pipeline_hour: int = 22
    pipeline_minute: int = 30
    publish_hour: int = 8
    publish_minute: int = 0
    sync_interval_min: int = 10
    # how often the eager sweep pre-processes freshly arrived clips (keyframes +
    # transcript + vision summary) so the 22:30 build isn't doing it all at once.
    # Triggers also fire it immediately on upload / Drive sync; this is the
    # self-healing safety net (covers bot-was-down / Drive-only arrivals).
    eager_interval_min: int = 3

    # False = the 22:30 job only sends a check-in reminder; the build starts
    # when the user sends /makeit
    auto_pipeline: bool = True

    # True = after drafting, STOP and send the script to Telegram for review;
    # voiceover + render run only once the script is approved ("✅ Approve &
    # build"). False = today's behavior (draft → straight to voiceover/render).
    script_review: bool = True

    # True = after drafting, run a second reasoning pass (the `humanizer` skill,
    # vendored to prompts/humanizer.md) that rewrites the spoken narration + the
    # X/LinkedIn captions to strip AI-writing patterns. Fresh drafts only — never
    # re-writes the founder's explicit revision wording. EN fields only.
    humanize: bool = True

    # True = the agent proposes 1-2s "hook" cold-opens (the most attention-
    # grabbing moment across the clips); the founder picks one in the script-
    # review gate and it's prepended (with the clip's natural sound) before the
    # narrated body. False = no hook proposal, no cold-open (today's behavior).
    first_frame_hook: bool = True

    notify_verbose: bool = True
    bilingual: bool = False  # optional: also produce a 2nd-language voiceover + video + captions
    burn_subtitles: bool = True  # burn ASS word subtitles into the video
    include_photos: bool = False  # photos (jpg/png/heic) as Ken Burns stills —
    # off: photos in the Drive folder are ignored, the video is clips-only

    # fixed footer appended to every published caption (all platforms, both
    # langs), after the body + any X shoutout. Empty string = no footer.
    post_signature_en: str = ""
    post_signature_es: str = ""

    # Which language(s) each platform PUBLISHES (one primary language per
    # platform beats duplicate same-account posts). Default: English on X,
    # Spanish on LinkedIn. Format "platform:lang[+lang],..."; platform absent
    # → English. `bilingual` still controls whether BOTH videos are generated
    # (needed: EN video for X, ES video for LinkedIn).
    publish_langs: str = "twitter:en,linkedin:es"

    # read-only web dashboard; empty password disables it entirely
    dashboard_password: str = ""
    dashboard_port: int = 8080

    # fast-preview render (scripts/preview.py): low-res + ultrafast for quick
    # visual QA, reusing the existing voiceover (no TTS, no Claude).
    preview_scale: str = "540:960"
    preview_preset: str = "ultrafast"

    data_dir: Path = Path("/data")
    whisper_model: str = "small"
    # subtitle/beat timing source: "whisperx" forced-aligns the known script
    # (tight); "whisper" reverts to free-form transcription (the old, drifty
    # path). Any other value or a missing torch install also falls back.
    subtitle_aligner: str = "whisperx"
    align_model_es: str | None = None  # override the default es wav2vec2 model
    max_video_seconds: int = 75

    # Narration word budget handed to the drafting prompt (≈ words ÷ (2.6·speed)
    # seconds spoken). The render hard-caps at max_video_seconds, so keep
    # script_words_max comfortably under that ceiling. Was 150-175 (~50-58s).
    script_words_min: int = 180
    script_words_max: int = 210

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def day_number(self, d: date) -> int:
        return (d - self.trip_start_date).days + 1

    def day_dir(self, d: date) -> Path:
        return self.data_dir / "days" / d.isoformat()


settings = Settings()
