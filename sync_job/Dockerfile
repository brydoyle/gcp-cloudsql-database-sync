FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Cloud Run Jobs run the container to completion; non-zero exit = failure.
ENTRYPOINT ["python", "main.py"]
