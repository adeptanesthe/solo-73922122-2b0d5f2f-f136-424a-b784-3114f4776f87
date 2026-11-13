FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY samples ./samples
COPY demo ./demo

ENV APP_DB=/data/app.db
VOLUME /data
EXPOSE 8000

# 同一镜像默认起一个进程；docker-compose 用不同端口再起第二个共库进程。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
