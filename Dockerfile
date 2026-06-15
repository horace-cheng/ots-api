FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH="/app/ots-common:$PYTHONPATH"

# Video assembly 需要 FFmpeg
# 只下載 NotoSerifCJKtc-Bold.otf（~15MB），不定義龐大的 fonts-noto-cjk（~200MB）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/share/fonts/opentype/noto \
    && curl -fsSL -o /usr/share/fonts/opentype/noto/NotoSerifCJKtc-Bold.otf \
       https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/OTF/TraditionalChinese/NotoSerifCJKtc-Bold.otf

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
