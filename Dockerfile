FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 paperlingo \
    && useradd --uid 10001 --gid paperlingo --create-home paperlingo

COPY --chown=paperlingo:paperlingo paperlingo/ ./paperlingo/
COPY --chown=paperlingo:paperlingo prompts/ ./prompts/
COPY --chown=paperlingo:paperlingo data/abstract_data.jsonl data/abstract_labels_v2.jsonl data/paperlingo-evaluation-labels-1044-grammar.jsonl data/labeling_prompt_v2.md ./data/
RUN mkdir -p /app/runtime && chown paperlingo:paperlingo /app/runtime

USER paperlingo
ENV PAPERLINGO_HOST=0.0.0.0 \
    PAPERLINGO_PORT=4173 \
    PAPERLINGO_RUNTIME_DIR=/app/runtime \
    PAPERLINGO_REVIEW_MODE=code
EXPOSE 4173
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD python -c "import json, urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:4173/api/health', timeout=2))['status'] == 'ok'"
CMD ["python", "-m", "paperlingo"]
