FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY s3_exporter.py .

EXPOSE 9340

ENTRYPOINT ["python", "s3_exporter.py"]
