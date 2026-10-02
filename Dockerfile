FROM node:24-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mediaresume ./mediaresume
COPY --from=web /web/dist ./web/dist
WORKDIR /app/config
EXPOSE 8095
CMD ["python", "-m", "mediaresume", "-c", "/app/config/config.yaml", "-p", "8095"]
