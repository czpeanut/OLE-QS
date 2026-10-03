# 選題組卷平台（FastAPI + headless Chromium 產 PDF）
# 題庫資料庫與圖檔不打包進映像，執行時掛載 data/（見 docker-compose.yml）。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OLEQS_DB=/app/data/oleqs.db OLEQS_ASSETS=/app/data/assets OLEQS_CURRICULUM=/app/data/curriculum

# 中文字型：排版 PDF 用 Noto Serif CJK，英數字用 Liberation Serif
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium

COPY apps ./apps
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
