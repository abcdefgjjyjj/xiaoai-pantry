FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PANTRY_HOST=0.0.0.0 PANTRY_PORT=8768 PANTRY_DATA_DIR=/data PANTRY_BACKUP_DIR=/backup TZ=Asia/Shanghai
RUN groupadd --gid 10001 pantry && useradd --uid 10001 --gid pantry --no-create-home pantry && mkdir /data /backup && chown pantry:pantry /data /backup
WORKDIR /app
COPY app.py pantry.py catalog.py gateway.py speaker.py backup.py page.html ./
USER pantry
EXPOSE 8768
CMD ["python", "app.py"]
