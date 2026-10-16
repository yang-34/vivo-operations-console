FROM node:22-alpine AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html tsconfig.json vite.config.ts ./
COPY public ./public
COPY src ./src
RUN npm run build

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 VIVO_BIND_HOST=0.0.0.0 VIVO_DATA_DIR=/app/data
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --uid 10001 --create-home vivo
COPY --chown=vivo:vivo server ./server
COPY --from=frontend --chown=vivo:vivo /build/dist ./dist
RUN mkdir -p /app/data && chown vivo:vivo /app/data
USER vivo
EXPOSE 8100
CMD ["python","server/app.py"]
