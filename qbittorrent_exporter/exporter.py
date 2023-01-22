import time
import os
import sys
import signal
import faulthandler
from qbittorrentapi import Client, TorrentStates
from qbittorrentapi.exceptions import APIConnectionError, HTTP404Error
import logging
from pythonjsonlogger import jsonlogger
from influx_line_protocol import Metric, MetricCollection
from http.server import BaseHTTPRequestHandler, HTTPServer
from functools import partial


# Enable dumps on stderr in case of segfault
faulthandler.enable()
logger = logging.getLogger()


class QbittorrentMetricsCollector(BaseHTTPRequestHandler):

    def __init__(self, config, *args, **kwargs):
        self.config = config
        self.timestamp = time.time_ns()
        self.client = Client(
            host=config["host"],
            port=config["port"],
            username=config["username"],
            password=config["password"],
        )
        super().__init__(*args, **kwargs)

    def do_GET(self):
        try:
            self.timestamp = time.time_ns()
            collection = MetricCollection()
            collection.metrics.extend(self.get_qbittorrent_status_metrics().metrics)
            collection.metrics.extend(self.get_qbittorrent_torrent_info().metrics)

            self.send_response(200)
            self.send_header("Content-type", "text/plain;charset=utf-8")
            self.end_headers()
            self.wfile.write(bytes(str(collection), "utf-8"))

        except HTTP404Error:
            logger.error("404 Error!")
        except APIConnectionError:
            logger.exception(f"Couldn't get server info:")
        except Exception:
            logger.exception("error!")
        else:
            return
        self.send_response(404)
        self.end_headers()

    # disable logging from server
    def log_request(self, code='-', size='-'):
        return

    def get_qbittorrent_status_metrics(self):
        transfer_info = self.client.transfer_info()
        tags = [
            "connection_status"
        ]
        values = [
            "dht_nodes",
            "dl_info_data",
            "up_info_data",
        ]
        collection = MetricCollection()
        metric = Metric(f"{self.config['metrics_prefix']}_transfer")
        metric.with_timestamp(self.timestamp)
        for tag in tags:
            metric.add_tag(tag, transfer_info[tag])
        for value in values:
            metric.add_value(value, transfer_info[value])
        collection.append(metric)
        return collection

    def get_qbittorrent_torrent_info(self):
        torrents = self.client.torrents.info(status_filter=["resumed"], SIMPLE_RESPONSES=True)
        torrent_values = [
            "uploaded",
            "downloaded",
            "dlspeed",
            "upspeed",
            "num_complete",
            "num_incomplete",
            "num_leechs",
            "num_seeds",
        ]
        torrent_tags = [
            "name",
            "hash",
            "tracker",
            "state",
            "category",
            "size",
            "added_on",
        ]
        peer_values = [
            "dl_speed",
            "downloaded",
            "uploaded",
            "up_speed",
            "progress",
        ]
        peer_tags = [
            "ip",
            "port",
            "flags",
            "client",
            "connection",
            "country",
        ]
        collection = MetricCollection()
        for t in torrents:
            metric = Metric(f"{self.config['metrics_prefix']}_torrent")
            if not t["category"]:
                t["category"] = "uncategorized"
            metric.with_timestamp(self.timestamp)
            for tag in torrent_tags:
                metric.add_tag(tag, t[tag])
            for value in torrent_values:
                metric.add_value(value, t[value])

            collection.append(metric)

            if self.config['log_peers']:
                if t['num_leechs']:
                    peers = self.client.sync.torrent_peers(torrent_hash=t['hash'])
                    for peer in peers['peers']:
                        metric = Metric(f"{self.config['metrics_prefix']}_peers")
                        metric.with_timestamp(self.timestamp)
                        metric.add_tag("hash", t["hash"])
                        for tag in peer_tags:
                            metric.add_tag(tag, peers["peers"][peer][tag])
                        for value in peer_values:
                            metric.add_value(value, peers["peers"][peer][value])
                        collection.append(metric)
        return collection


class SignalHandler():
    def __init__(self):
        self.shutdownCount = 0

        # Register signal handler
        signal.signal(signal.SIGINT, self._on_signal_received)
        signal.signal(signal.SIGTERM, self._on_signal_received)

    def is_shutting_down(self):
        return self.shutdownCount > 0

    def _on_signal_received(self, signal, frame):
        if self.shutdownCount > 1:
            logger.warning("Forcibly killing exporter")
        logger.info("Exporter is shutting down")
        self.shutdownCount += 1
        sys.exit(1)


def get_config_value(key, default=""):
    input_path = os.environ.get("FILE__" + key, None)
    if input_path is not None:
        try:
            with open(input_path, "r") as input_file:
                return input_file.read().strip()
        except IOError as e:
            logger.error(f"Unable to read value for {key} from {input_path}: {str(e)}")

    return os.environ.get(key, default)


def main():
    # Init logger so it can be used
    logHandler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime) %(levelname) %(message)",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logHandler.setFormatter(formatter)
    logger.addHandler(logHandler)
    logger.setLevel("INFO") # default until config is loaded

    config = {
        "host": get_config_value("QBITTORRENT_HOST", "192.168.1.49"),
        "port": int(get_config_value("QBITTORRENT_PORT", "8080")),
        "username": get_config_value("QBITTORRENT_USER", "admin"),
        "password": get_config_value("QBITTORRENT_PASS", "adminadmin"),
        "exporter_port": int(get_config_value("EXPORTER_PORT", "8000")),
        "log_level": get_config_value("EXPORTER_LOG_LEVEL", "INFO"),
        "metrics_prefix": get_config_value("METRICS_PREFIX", "qbittorrent"),
        "log_peers": get_config_value("LOG_PEERS", "false").lower() == "true",
    }

    # set level once config has been loaded
    logger.setLevel(config["log_level"])

    # Register signal handler
    signal_handler = SignalHandler()

    if not config["host"]:
        logger.error("No host specified, please set QBITTORRENT_HOST environment variable")
        sys.exit(1)
    if not config["port"]:
        logger.error("No post specified, please set QBITTORRENT_PORT environment variable")
        sys.exit(1)

    # Register our custom collector
    logger.info("Exporter is starting up")

    # Start server
    handler = partial(QbittorrentMetricsCollector, config)
    httpd = HTTPServer(("", config["exporter_port"]), handler)
    logger.info(f"Exporter listening on port {config['exporter_port']}")
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit) as e:
        pass
    httpd.server_close()
    logger.info("Exporter has shutdown")
