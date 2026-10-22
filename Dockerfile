FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 PYTHONPATH=/app
WORKDIR /project
COPY tool /app/tool
COPY tests /app/tests
COPY templates /app/templates
COPY container_entrypoint.py /app/container_entrypoint.py
ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["python", "-m", "tool"]
