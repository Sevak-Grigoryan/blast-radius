FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# the exact packages sqlglot's tests were labeled with, so the selector can run tests itself
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.lock
COPY src.tar.gz selector.py verify.py map.json.gz implines.json.gz vcost.json /app/
# sqlglot and its tests at the pinned commit (git archive of eb11aec), byte-compiled ahead
RUN python -c "import tarfile; tarfile.open('/app/src.tar.gz').extractall('/app/sqlglot', filter='data')" \
 && rm /app/src.tar.gz && test -f /app/sqlglot/sqlglot/parser.py \
 && (python -m compileall -q -j 4 /app/sqlglot/sqlglot /app/sqlglot/tests > /dev/null || true) \
 && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
