# --- Stage 1: build the React frontend ---
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json ./
RUN npm install
COPY frontend/public/ ./public/
COPY frontend/src/ ./src/
RUN npm run build

# --- Stage 2: Python runtime (no Node/npm shipped here) ---
FROM python:3.11-slim
WORKDIR /app

COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=frontend-build /app/frontend/build/ ./backend/static/
RUN chmod +x backend/startup.sh

WORKDIR /app/backend
CMD ["./startup.sh"]
