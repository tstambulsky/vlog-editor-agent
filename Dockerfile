# trixie ships ffmpeg 7.x (needed for proper iPhone Dolby Vision handling)
FROM python:3.12-slim-trixie AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg rclone fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1

# Production image: base + the forced-alignment stack (subtitle/beat timing,
# app/services/align.py). CPU-only torch so we don't pull the multi-GB CUDA
# wheels. The torch/whisperx install sits BEFORE `COPY app` so editing code
# never re-downloads it on a server `docker compose build`. Pinned to the cpu
# wheels whisperx 3.8.6 resolves to, installed from the cpu index FIRST so the
# later whisperx install finds them satisfied and never swaps in a CUDA build
# (~6GB). Verified: torch 2.8.0+cpu, cuda None, ~2.2GB image.
# The wav2vec2 align models are NOT baked in: the compose volume
# ./data/hf-cache shadows the cache dir at runtime, so they download once into
# that persisted volume (same as faster-whisper). TORCH_HOME points there too.
#   Pre-warm after deploy:
#   docker compose run --rm --no-deps video-journal python -c \
#     "import whisperx; whisperx.load_align_model(language_code='en',device='cpu'); \
#      whisperx.load_align_model(language_code='es',device='cpu')"
FROM base AS prod
ENV HF_HOME=/root/.cache/huggingface \
    TORCH_HOME=/root/.cache/huggingface/torch
RUN pip install --no-cache-dir \
        torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir whisperx==3.8.6
COPY app ./app
COPY prompts ./prompts
COPY scripts ./scripts
CMD ["python", "-m", "app.main"]

# Test image (no torch -> exercises the whisper fallback):
#   docker build --target dev -t video-journal-dev . && docker run --rm video-journal-dev
FROM base AS dev
RUN pip install --no-cache-dir pytest
COPY app ./app
COPY prompts ./prompts
COPY scripts ./scripts
COPY pytest.ini .
COPY tests ./tests
CMD ["pytest", "-q"]
