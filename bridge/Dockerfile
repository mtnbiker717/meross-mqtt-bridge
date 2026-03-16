FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py .
RUN addgroup --system --gid 1000 app && \
    adduser --system --uid 1000 --ingroup app app
USER app
CMD ["python", "-u", "bridge.py"]
