FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY config.py .

EXPOSE 8000

CMD ["python", "server.py"]