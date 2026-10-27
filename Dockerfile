# Playwright image already contains Chromium + system libraries.
# Tag must match the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY corium_bot.py .

CMD ["python", "corium_bot.py"]
