FROM node:22.20.0-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/app.js ./
RUN npm run build

FROM python:3.12.12-slim-bookworm AS app

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 PYTHONPATH=/app
WORKDIR /project
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY tool /app/tool
COPY tests /app/tests
COPY templates /app/templates
COPY docker/container_entrypoint.py /app/container_entrypoint.py
ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["python", "-m", "tool"]

FROM python:3.12.12-slim-bookworm AS texlive
RUN apt-get update && apt-get install -y --no-install-recommends perl curl xz-utils ca-certificates && rm -rf /var/lib/apt/lists/*
COPY docker/install-texlive.sh /tmp/install-texlive.sh
RUN sh /tmp/install-texlive.sh

FROM app AS web
RUN apt-get update && apt-get install -y --no-install-recommends perl && rm -rf /var/lib/apt/lists/*
COPY --from=texlive /opt/texlive /opt/texlive
COPY --from=frontend /src/tool/web_assets/app.js /app/tool/web_assets/app.js
ENV PATH="/opt/texlive/bin/current:${PATH}" TEXLIVE_MANIFEST=/opt/texlive/manifest.txt DM_WEB_ASSETS=/app/tool/web_assets
EXPOSE 8080
CMD ["python", "-m", "tool.web", "--host", "0.0.0.0"]
