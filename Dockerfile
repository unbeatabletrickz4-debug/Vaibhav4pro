FROM python:3.10-slim

WORKDIR /app

# Install Node.js, Git, and System Tools
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    procps \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]
