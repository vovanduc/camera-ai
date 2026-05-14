ARG BUILD_FROM=python:3.11-alpine
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py run.sh ./
RUN chmod +x /app/run.sh

EXPOSE 8000

CMD ["/app/run.sh"]
