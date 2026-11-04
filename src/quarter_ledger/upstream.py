"""Keep-alive connections to vendors, so a call skips a new TLS handshake (20 to 35 ms).

Connections are kept per scheme, host and port, never shared between hosts. One goes back
to its pool only once its reply, streamed or not, has been read to the end with no error.
At most `size` idle connections are kept per host, each for at most `idle` seconds, and an
idle one the vendor has closed is dropped before it is used.

The double-charge rule: a call is sent again, once and on a new connection, only when a
reused connection failed before any request byte was confirmed written and before any reply
byte came back. That is a socket the vendor closed while it sat idle: the vendor never had
the request. A failure after that is never retried, since the vendor may have done the work.
"""

import base64
import http.client
import ssl
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

TLS = ssl.create_default_context()  # verifies hosts, as urllib's default does
TLS.set_alpn_protocols(["http/1.1"])
USER_AGENT = f"Python-urllib/{sys.version_info.major}.{sys.version_info.minor}"  # what vendors saw before


class Unsent(Exception):
    """The request never fully left: the vendor cannot have acted on it."""


class Sent(Exception):
    """The request was written and no whole reply came back: the vendor may have acted on it."""


class _Counted:
    written = False  # set once a send completes: from then on the vendor may have the request

    def send(self, data):
        super().send(data)
        self.written = True


class _HTTP(_Counted, http.client.HTTPConnection):
    pass


class _HTTPS(_Counted, http.client.HTTPSConnection):
    pass


def _alive(conn) -> bool:
    """Whether an idle connection is still open: nothing to read yet, and no end of file."""
    sock = conn.sock
    if sock is None:
        return False
    try:
        sock.setblocking(False)
        sock.recv(1)  # b"" is a close; any byte is one the vendor should not have sent
        return False
    except (BlockingIOError, ssl.SSLWantReadError):
        return True
    except OSError:
        return False
    finally:
        try:
            sock.settimeout(conn.timeout)
        except OSError:
            pass


class Pool:
    def __init__(self, size: int = 8, idle: float = 30.0, timeout: float = 300):
        self.size, self.idle, self.timeout = size, idle, timeout
        self._idle: dict[tuple, list[tuple[http.client.HTTPConnection, float]]] = {}
        self._lock = threading.Lock()
        self.connects = 0  # new connections made, for tests and measurement

    @contextmanager
    def open(self, method: str, url: str, body: bytes | None, headers: dict):
        """Send one request and yield the reply. Raises Unsent or Sent instead of a reply."""
        parts = urlsplit(url)
        key = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = {"user-agent": USER_AGENT, **headers}
        if body is not None and "content-type" not in headers:
            headers["content-type"] = "application/x-www-form-urlencoded"  # as urllib did
        conn, resp = self._send(key, method, path, body, headers)
        done = False
        try:
            yield resp
            done = True
        finally:
            self._put(key, conn, resp, done)

    def _send(self, key, method, path, body, headers):
        for retry in (False, True):
            conn, reused = (self._new(key), False) if retry else self._take(key)  # a retry: always new
            conn.written = False  # of this request: a reused connection wrote earlier ones
            try:
                conn.request(method, path, body, headers)
            except (OSError, http.client.HTTPException, ValueError) as e:
                conn.close()
                if reused and not conn.written and not retry:
                    continue  # closed while idle, before the vendor had a byte of this call
                raise (Sent if conn.written else Unsent)(e) from e
            try:
                return conn, conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                conn.close()  # written, so never sent again
                raise Sent(e) from e
        raise AssertionError("unreachable")

    def _take(self, key) -> tuple[http.client.HTTPConnection, bool]:
        now = time.monotonic()
        with self._lock:
            idle = self._idle.get(key, [])
            while idle:
                conn, since = idle.pop()  # the most recently used first
                if now - since < self.idle and _alive(conn):
                    return conn, True
                conn.close()
        return self._new(key), False

    def _new(self, key) -> http.client.HTTPConnection:
        scheme, host, port = key
        with self._lock:
            self.connects += 1
        if scheme != "https":  # plain http is only ever a local upstream
            return _HTTP(host, port, timeout=self.timeout)
        proxy = urllib.request.getproxies().get("https")
        if not proxy or urllib.request.proxy_bypass(host):
            return _HTTPS(host, port, timeout=self.timeout, context=TLS)
        p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")  # HTTPS_PROXY, as urllib honoured
        conn = _HTTPS(p.hostname, p.port or 80, timeout=self.timeout, context=TLS)
        auth = {}
        if p.username:
            pair = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
            auth["Proxy-Authorization"] = "Basic " + base64.b64encode(pair).decode()
        conn.set_tunnel(host, port, auth)
        return conn

    def _put(self, key, conn, resp, done: bool) -> None:
        """Back to the pool only when the reply was read to the end and the vendor keeps
        the connection open; otherwise closed."""
        if not (done and resp.isclosed() and conn.sock is not None):
            conn.close()
            return
        now, drop = time.monotonic(), []
        with self._lock:
            idle = self._idle.setdefault(key, [])
            drop = [c for c, since in idle if now - since >= self.idle]
            idle[:] = [(c, since) for c, since in idle if now - since < self.idle]
            if len(idle) < self.size:
                idle.append((conn, now))
            else:
                drop.append(conn)
        for c in drop:
            c.close()

    def close(self) -> None:
        with self._lock:
            pools, self._idle = self._idle, {}
        for idle in pools.values():
            for conn, _ in idle:
                conn.close()
