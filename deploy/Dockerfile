FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gnupg wget \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY crawler/requirements.txt crawler/requirements.txt
RUN pip install --no-cache-dir -r crawler/requirements.txt
COPY . .

COPY deploy/entrypoint.sh /usr/local/bin/gearfire-entrypoint
RUN chmod +x /usr/local/bin/gearfire-entrypoint

ENTRYPOINT ["/usr/local/bin/gearfire-entrypoint", "python", "-m", "deploy.runner"]
CMD ["serve"]
