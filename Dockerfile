FROM python:3.12-slim

# 避免 Python 輸出被 buffer，讓 Cloud Run log 即時顯示
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先複製 requirements，利用 Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式碼
COPY . .

# Cloud Run 預設 port 8080
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
