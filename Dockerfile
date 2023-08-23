# Use an official Python base image
FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    bzip2 \
    libffi-dev \
    openssl \
    sqlite3 libsqlite3-dev \
    tk-dev \
    tzdata \
    xz-utils \
    zlib1g-dev \
    ffmpeg \ 
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Command to run your bot
CMD ["python", "Bot.py"]