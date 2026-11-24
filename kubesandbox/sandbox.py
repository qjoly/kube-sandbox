"""Micro-VM sandbox lifecycle on Kubernetes: claim, resolve, exec, delete.

The cluster's agent-sandbox controller hands out a pod from a warm pool for each SandboxClaim, so
a sandbox is three REST calls plus a wait: create the claim, read ``status.sandbox.name`` off it,
delete the claim. Commands then ride the exec stream in :mod:`kubesandbox.protocol`.

Shared by the Hermes terminal backend (``kube-sandbox``), the SSH transport and any CLI wrapper, so
that provisioning cannot drift between them.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass

from .protocol import ApiError, KubeClient, PodTarget

__all__ = ["Claim", "SandboxClient", "Submitted"]

CLAIM_API = "/apis/extensions.agents.x-k8s.io/v1beta1"
_NAME_OK = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _clean_name(value: str, *, max_len: int = 63) -> str:
    """DNS-1123 label derived from *value*: task ids and session ids carry ``_`` and long hashes."""
    name = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    return name[:max_len].strip("-") or "sandbox"


@dataclass(frozen=True)
class Submitted:
    """A detached command: its output and exit status land in files inside the sandbox."""

    output_path: str
    exit_path: str


@dataclass(frozen=True)
class Claim:
    namespace: str
    name: str
    sandbox: str  # the pod the controller handed out
    container: str


class SandboxClient:
    def __init__(self, client: KubeClient, *, namespace: str = "sandboxes", template: str = "dev",
                 container: str = "dev", start_timeout: float = 180.0, ttl_seconds: int = 7 * 24 * 3600):
        self.client = client
        self.namespace = namespace
        self.template = template
        self.container = container
        self.start_timeout = start_timeout
        self.ttl_seconds = ttl_seconds

    # -- lifecycle ---------------------------------------------------------
    def claim_body(self, name: str, ttl_seconds: int | None = None) -> dict:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        deadline = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl))
        return {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {"name": name},
            "spec": {
                "warmPoolRef": {"name": self.template},
                "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": deadline},
            },
        }

    def create(self, name: str, *, ttl_seconds: int | None = None) -> Claim:
        """Create a claim and wait for its pod. ``AlreadyExists`` (409) means reuse it."""
        name = _clean_name(name)
        path = f"{CLAIM_API}/namespaces/{self.namespace}/sandboxclaims"
        try:
            obj = self.client.call("POST", path, body=self.claim_body(name, ttl_seconds), timeout=30)
        except ApiError as exc:
            if exc.status != 409:
                raise
            obj = self.client.call("GET", f"{path}/{name}")
        return self.wait(name, obj)

    def get(self, name: str) -> Claim | None:
        path = f"{CLAIM_API}/namespaces/{self.namespace}/sandboxclaims/{_clean_name(name)}"
        try:
            obj = self.client.call("GET", path)
        except ApiError as exc:
            if exc.not_found:
                return None
            raise
        return self.wait(name, obj)

    def wait(self, name: str, obj: dict | None = None) -> Claim:
        path = f"{CLAIM_API}/namespaces/{self.namespace}/sandboxclaims/{_clean_name(name)}"
        deadline = time.monotonic() + self.start_timeout
        while True:
            if obj is None:
                obj = self.client.call("GET", path)
            status = obj.get("status") or {}
            conditions = {c.get("type"): c for c in status.get("conditions") or []}
            ready = conditions.get("Ready") or {}
            sandbox = (status.get("sandbox") or {}).get("name")
            if sandbox and ready.get("status") == "True":
                return Claim(self.namespace, obj["metadata"]["name"], sandbox, self.container)
            if str(ready.get("status")).lower() == "false" and "error" in str(ready.get("reason", "")).lower():
                raise ApiError(0, f"sandbox claim {name} failed: {ready.get('reason')} {ready.get('message')}".strip())
            if time.monotonic() >= deadline:
                raise ApiError(0, f"sandbox claim {name} was not ready within {self.start_timeout:.0f}s "
                                  f"(reason: {ready.get('reason') or 'none'})")
            time.sleep(0.5)
            obj = None

    def remove(self, name: str) -> bool:
        """Delete a claim; ``False`` when it was already gone (cleanup must not raise for that)."""
        path = f"{CLAIM_API}/namespaces/{self.namespace}/sandboxclaims/{_clean_name(name)}"
        try:
            self.client.call("DELETE", path, body={"propagationPolicy": "Background"}, timeout=30)
            return True
        except ApiError as exc:
            if exc.not_found:
                return False
            raise

    def list_claims(self) -> list[dict]:
        obj = self.client.call("GET", f"{CLAIM_API}/namespaces/{self.namespace}/sandboxclaims")
        return obj.get("items") or []

    def exists(self, name: str) -> bool:
        path = f"{CLAIM_API}/namespaces/{self.namespace}/sandboxclaims/{_clean_name(name)}"
        try:
            self.client.call("GET", path, timeout=10)
            return True
        except ApiError as exc:
            if exc.not_found:
                return False
            raise

    # -- secret files --------------------------------------------------------
    def write_secret_file(self, claim: Claim, *, path: str, content: str, mode: int = 0o600) -> None:
        """Write *content* to *path* inside the sandbox with *mode* (default 0600).

        The value travels base64-encoded inside a single exec script: it never appears in the
        process argv of the exec stream nor in the sandbox environment, only in the target file.
        """
        import base64
        blob = base64.b64encode(content.encode()).decode()
        script = (
            "set -e\n"
            f"umask 077\n"
            f"mkdir -p {_q(os.path.dirname(path) or '/')}\n"
            f"printf '%s' {_q(blob)} | base64 -d > {_q(path)}\n"
            f"chmod {mode:o} {_q(path)}\n"
        )
        out, rc = self.exec(claim, ["bash", "-c", script], timeout=30)
        if rc != 0:
            raise ApiError(rc, f"writing {path} in sandbox failed: {out.decode('utf-8', 'replace')[:200]}")

    def seed_git_credentials(self, claim: Claim, *, username: str, token: str,
                             host: str = "github.com") -> None:
        """Convenience wrapper: ~/.git-credentials + credential.helper, file-only (never env/argv)."""
        self.write_secret_file(claim, path="/root/.git-credentials",
                               content=f"https://{username}:{token}@{host}\n")
        script = (
            "git config --global credential.helper 'store --file=/root/.git-credentials'\n"
            f"git config --global user.name {_q(username)}\n"
        )
        out, rc = self.exec(claim, ["bash", "-c", script], timeout=30)
        if rc != 0:
            raise ApiError(rc, f"git config failed: {out.decode('utf-8', 'replace')[:200]}")

    # -- execution ---------------------------------------------------------
    def exec(self, claim: Claim, command: list[str], *, timeout: float = 120.0,
             stdin_data: bytes | None = None) -> tuple[bytes, int]:
        return self.client.exec_ws(PodTarget(claim.namespace, claim.sandbox, claim.container), command,
                                   stdin_data=stdin_data, timeout=timeout)

    def submit(self, claim: Claim, command: list[str], *, name: str | None = None) -> Submitted:
        """Detach *command*: its output and exit status land in files inside the sandbox.

        The exec stream is torn down when its client goes away, so a background command cannot ride
        it; a detached process writing to files can be polled through ordinary commands instead.
        """
        token = _clean_name(name or uuid.uuid4().hex[:8])
        out = f"/tmp/.hermes-bg/{token}.log"
        status = f"/tmp/.hermes-bg/{token}.rc"
        inner = " ".join(_q(part) for part in command)
        script = (
            f"mkdir -p /tmp/.hermes-bg && : > {_q(out)} && rm -f {_q(status)}\n"
            f"( {inner} ) > {_q(out)} 2>&1\n"
            f"echo $? > {_q(status)}.tmp && mv {_q(status)}.tmp {_q(status)}\n"
        )
        # setsid detaches it from the exec stream's pty and session, so the command outlives this call:
        # the shell forks it and exits at once, no pipe is left for the stream to hold open.
        self.client.exec_ws(PodTarget(claim.namespace, claim.sandbox, self.container),
                            ["bash", "-c", f"setsid bash -c {_q(script)} </dev/null >/dev/null 2>&1 &"], timeout=30)
        return Submitted(out, status)


def _q(value: str) -> str:
    import shlex
    return shlex.quote(value)
