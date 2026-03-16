FROM node:20-slim AS css-builder
WORKDIR /build
COPY package.json tailwind.config.js input.css ./
COPY templates ./templates
COPY static ./static
RUN npm install && npm run build

FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && gosu nobody true

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=css-builder /build/static/app.css /app/static/app.css
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
