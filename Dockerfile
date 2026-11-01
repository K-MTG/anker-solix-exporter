FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check -r requirements.txt

# Only the application code, so deployment files next to it (the env file with the token) stay out of the image.
COPY exporter.py /app/

CMD ["python", "exporter.py"]
