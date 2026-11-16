# Windy — single-container build: FastAPI backend + built Vue frontend.
# The frontend dist is served by the backend itself (http://localhost:8000).
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN corepack enable && corepack pnpm install --frozen-lockfile
COPY frontend/ ./
RUN corepack pnpm build

FROM python:3.12-slim
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=frontend /app/frontend/dist /app/frontend/dist

ENV WINDY_DB_PATH=/data/windy.db
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
