FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py .
RUN addgroup --system app && adduser --system --ingroup app app
USER app
CMD ["python", "-u", "bridge.py"]
