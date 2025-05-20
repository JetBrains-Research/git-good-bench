FROM --platform=linux/amd64 python:3.10-slim
LABEL authors="tobias.lindenbauer"

WORKDIR /usr/code/
COPY repository_scraper_requirements_linux.txt .

# Install git and prune unneeded packages
RUN apt-get update && apt-get install -y git \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global user.name "vcs-agent" && \
    git config --global user.email "vcs@agent.takeover"

# Install project dependencies including YSON and YTclient
RUN pip install --no-cache-dir -r repository_scraper_requirements_linux.txt
