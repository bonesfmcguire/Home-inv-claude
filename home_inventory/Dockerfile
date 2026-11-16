FROM python:3.12-alpine
WORKDIR /app
COPY server.py ./
COPY static ./static
ENV DATA_DIR=/data PORT=8099 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8099
CMD ["python3", "/app/server.py"]
