FROM python:3.12-slim

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY lgtv_exporter.py .

USER nobody:nogroup
EXPOSE 9095
CMD ["python3", "lgtv_exporter.py"]
