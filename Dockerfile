FROM python:3.11-slim

# tzdata — чтобы TZ из docker-compose.yml работал: время в именах скринов
# считается по местному времени.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir opencv-python-headless==4.10.0.84 "numpy<2.3" qrcode==8.0

WORKDIR /app
COPY screen_script.py gui.py gui.html login.html browse.html ./

ENV PYTHONUNBUFFERED=1 \
    SCREEN_GUI_HOST=0.0.0.0 \
    SCREEN_GUI_PORT=80 \
    SCREEN_SETTINGS_FILE=/config/settings.json \
    SCREEN_DRIVES_ROOT=/drives \
    SCREEN_BROWSE_ROOT=/drives \
    SCREEN_PICKER_HOST=host.docker.internal \
    SCREEN_INPUT_FOLDER=/drives/d/recordings \
    SCREEN_OUTPUT_FOLDER=/drives/d/screenshots \
    SCREEN_APPROVED_FOLDER=/drives/d/screenshots_approved \
    SCREEN_DELETED_FOLDER=/drives/d/screenshots_deleted \
    SCREEN_LOGS_FOLDER=/drives/d/screenshots_logs

EXPOSE 80
CMD ["python", "gui.py"]
