FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
WORKDIR /app/src

# Overridden by docker-compose for the mock PI API and dashboard services.
CMD ["python", "main.py"]
