# =====================================================================
# صورة واحدة تخدم الويب والـWorker — يختلفان في الأمر لا في المحتوى.
# =====================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Muscat

# WeasyPrint يحتاج Pango/Cairo لتصيير PDF، وخطوط Noto العربية ضرورية:
# بدونها تخرج صفحات PDF فارغة أو مربعات بدل الحروف.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        shared-mime-info \
        fonts-noto-core \
        fonts-noto-color-emoji \
        postgresql-client \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /srv/app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY worker ./worker
COPY scripts ./scripts

# لا يعمل شيء بصلاحية الجذر.
RUN useradd --system --create-home --uid 10001 hydrawise \
    && chown -R hydrawise:hydrawise /srv/app
USER hydrawise

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
