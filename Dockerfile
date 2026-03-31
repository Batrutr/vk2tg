FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

WORKDIR /app

# Фиксируем версию uv для воспроизводимых сборок.
ARG UV_VERSION=0.6.14

# Устанавливаем uv и зависимости из pyproject.toml/uv.lock.
RUN pip install --no-cache-dir uv==${UV_VERSION}
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

WORKDIR /app

# Системный пользователь без root-прав.
RUN groupadd -r app && useradd -r -g app app

# Копируем только готовое виртуальное окружение из builder-слоя.
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Затем копируем код проекта.
COPY --chown=app:app . .

USER app

CMD ["python", "main.py"]
