"""Offline tests: no cluster, no Hermes host. Run with ``python -m pytest`` from the repo root."""

import json

import pytest

from kubesandbox.protocol import ApiError, KubeClient, _exit_from_error, _dechunk


class FakeSock:
    """Stream socket double: request bytes out, scripted response chunks in."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def settimeout(self, _):
        pass

    def close(self):
        self.closed = True

    def shutdown(self, _):
        pass


def client_with(chunks):
    kc = KubeClient(server="https://api.example:443", token="t0ken")
    sock = FakeSock(chunks)
    kc.connect = lambda timeout: sock
    return kc, sock


def test_rest_call_reads_content_length_body():
    body = json.dumps({"items": [{"metadata": {"name": "a"}}]}).encode()
    kc, _ = client_with([b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body])
    assert kc.call("GET", "/api/v1/pods")["items"][0]["metadata"]["name"] == "a"


def test_rest_call_sends_bearer_and_no_upgrade_headers():
    kc, sock = client_with([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"])
    kc.call("GET", "/version")
    assert b"Authorization: Bearer t0ken" in sock.sent
    assert b"Upgrade:" not in sock.sent
    assert b"Connection: close" in sock.sent


def test_rest_call_raises_api_error_with_message():
    msg = json.dumps({"message": "sandboxclaims ... not found"}).encode()
    kc, _ = client_with([b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n\r\n" % len(msg) + msg])
    with pytest.raises(ApiError) as exc:
        kc.call("GET", "/x")
    assert exc.value.not_found and "not found" in exc.value.message


def test_chunked_body_is_decoded_whole():
    assert _dechunk(b"4\r\n{\"a\"\r\n3\r\n:1}\r\n0\r\n\r\n") == b"{\"a\":1}"
    assert _dechunk(b"4\r\n{\"a\"") == b""  # truncated tail: dropped, never a bogus size


def test_exec_upgrade_headers_are_exactly_the_negotiated_set():
    # The API server answers 406 to any Accept header on an upgrade and 400 to reordered fields:
    # this pins the shape that was verified against the real cluster.
    kc, sock = client_with([b"HTTP/1.1 400 Bad Request\r\nContent-Length: 6\r\n\r\nnope\r\n"])
    with pytest.raises(ApiError):
        kc.exec_ws(pytest.importorskip("kubesandbox.protocol").PodTarget("ns", "pod", "c"), ["bash", "-c", "id"])
    head = sock.sent.split(b"\r\n\r\n")[0].decode()
    lines = head.split("\r\n")
    assert not any(l.lower().startswith("accept:") for l in lines)
    order = [l.split(":")[0] for l in lines if ":" in l and l.split(":")[0] in
             ("Upgrade", "Connection", "Sec-WebSocket-Version", "X-Stream-Protocol-Version", "Sec-WebSocket-Key")]
    assert order == ["Upgrade", "Connection", "Sec-WebSocket-Version", "X-Stream-Protocol-Version",
                     "Sec-WebSocket-Key"]


@pytest.mark.parametrize("message,expected", [
    ("command terminated with non-zero exit code: error executing command [bash -c exit 7], exit code 7", 7),
    ("error executing command: exit code 126", 126),
    ("container not found", None),
])
def test_exit_status_comes_from_the_error_channel(message, expected):
    assert _exit_from_error(message) == expected


def test_kubeconfig_with_exec_plugin_is_refused(tmp_path):
    cfg = tmp_path / "kc"
    cfg.write_text(
        "apiVersion: v1\ncurrent-context: c\nclusters:\n- name: cl\n  cluster: {server: 'https://x:6443'}\n"
        "contexts:\n- name: c\n  context: {cluster: cl, user: u}\n"
        "users:\n- name: u\n  user:\n    exec: {command: kubelogin}\n")
    from kubesandbox.protocol import resolve_kubeconfig
    with pytest.raises(ApiError, match="exec plugin"):
        resolve_kubeconfig(str(cfg))
