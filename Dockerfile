FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
LABEL org.opencontainers.image.source="https://github.com/julianfnunez/scene-check"
LABEL org.opencontainers.image.title="Scene Watch"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY agent.py actions.py action_planner.py narration.py planner.py scheduler.py sensors.py service.py settings.py web.py ./
COPY mcp_servers ./mcp_servers
COPY static ./static
COPY .env.defaults ./.env.defaults
COPY scenes.example.yaml ./scenes.yaml
COPY sensors.example.yaml ./sensors.yaml
ENV DATA_DIR=/data PORT=8081
EXPOSE 8081
CMD [".venv/bin/python", "web.py"]
