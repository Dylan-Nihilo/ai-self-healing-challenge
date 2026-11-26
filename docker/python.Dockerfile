FROM python:3.11-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 workstation && mkdir -p /data && chown workstation:workstation /data
COPY shared ./shared

FROM base AS core
COPY core ./core
COPY seed ./seed
USER workstation
CMD ["python", "-m", "uvicorn", "core.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runner
COPY solution/requirements.txt /tmp/solution-requirements.txt
RUN pip install --no-cache-dir -r /tmp/solution-requirements.txt
COPY runner ./runner
COPY solution ./solution
USER workstation
CMD ["python", "-m", "uvicorn", "runner.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8200"]

FROM base AS tests
COPY solution/requirements.txt /tmp/solution-requirements.txt
RUN pip install --no-cache-dir -r /tmp/solution-requirements.txt
COPY core ./core
COPY seed ./seed
COPY runner ./runner
COPY solution ./solution
COPY assessment ./assessment
COPY tests ./tests
USER workstation
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
