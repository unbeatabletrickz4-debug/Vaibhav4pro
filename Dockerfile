FROM python:3.10-slim

WORKDIR /app

# 1. Install Basic Tools & Git
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    procps \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Node.js (v18)
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y nodejs

# 3. Copy Files
COPY . .

# 4. Install Python Deps
RUN pip install --no-cache-dir -r requirements.txt

# 5. Run Bot
CMD ["python", "bot.py"]
