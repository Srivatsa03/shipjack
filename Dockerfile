# One image for both Python services; the command picks which one runs:
#   store:     shipjack-serve --host 0.0.0.0 --port $PORT
#   assistant: shipjack-assistant --host 0.0.0.0 --port $PORT
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/models PORT=8000

# git: the AP2 SDK is installed from its GitHub commit
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, in their own layer: a code change doesn't reinstall them.
COPY pyproject.toml README.md ./
RUN mkdir shipjack && touch shipjack/__init__.py && mkdir shipjack/assistant && touch shipjack/assistant/__init__.py \
    && pip install ".[serve,assistant,postgres]" "model2vec>=0.4" && pip uninstall -y shipjack

# Bake the embedding model into the image, so a cold start doesn't download it.
RUN python -c "from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-base-8M')"

# Then the code (seconds to rebuild).
COPY shipjack ./shipjack
RUN pip install --no-deps .

RUN useradd --create-home app && chown -R app /opt/models
USER app

EXPOSE 8000
CMD ["sh", "-c", "shipjack-serve --host 0.0.0.0 --port ${PORT}"]
