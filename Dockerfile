FROM python:3.14-slim

WORKDIR /app

COPY app.py .
COPY index.html .
COPY manager.html .
COPY tablekeeper.db .

EXPOSE 8000

CMD ["python", "app.py"]
