FROM python:3.11-bullseye

# Install package
WORKDIR /code
COPY . .
RUN pip3 install .

ENV QBITTORRENT_HOST="localhost"
ENV QBITTORRENT_PORT="8080"
ENV QBITTORRENT_USER="admin"
ENV QBITTORRENT_PASS="adminadmin"
ENV EXPORTER_PORT="8000"
ENV EXPORTER_LOG_LEVEL="INFO"

ENTRYPOINT ["qbittorrent-exporter"]
