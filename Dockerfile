FROM python:3.10-slim

WORKDIR /app

# Install System Tools & Node.js
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    procps \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]
