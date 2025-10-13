FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    make \
    libjpeg-dev \
    zlib1g-dev \
    gcc \
    g++ \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt makefile ./
RUN make install

WORKDIR /app
COPY . /app

ENTRYPOINT [ "make" ] 
CMD [ "run" ]
