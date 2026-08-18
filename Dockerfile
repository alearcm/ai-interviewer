FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY harness/ harness/
COPY webui/ webui/
COPY packs/ packs/
COPY tools/ tools/
COPY regrade.py config.toml ./

EXPOSE 8765
CMD ["python", "-m", "harness", "web", "--host", "0.0.0.0"]
