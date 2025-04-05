FROM python:3.12

RUN apt-get update && apt-get install -y gcc

RUN pip install uv

WORKDIR /app
COPY pyproject.toml .

RUN uv sync

COPY src src

WORKDIR /app/src

CMD ["uv", "run", "python", "main.py"]