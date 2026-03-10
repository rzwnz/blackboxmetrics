FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tomcat_exporter.py .

EXPOSE 9341

ENTRYPOINT ["python", "tomcat_exporter.py"]
