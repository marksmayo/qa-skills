import time
import urllib.request

from ferry_booking.status_server import start


def test_health_endpoint_answers():
    srv = start(8765)
    try:
        time.sleep(0.4)
        assert urllib.request.urlopen("http://127.0.0.1:8765/").read() == b"ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_health_endpoint_status_code():
    srv = start(8765)
    try:
        time.sleep(0.4)
        assert urllib.request.urlopen("http://127.0.0.1:8765/").status == 200
    finally:
        srv.shutdown()
        srv.server_close()
