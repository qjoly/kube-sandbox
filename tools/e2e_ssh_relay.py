"""End-to-end: host-side ``ssh``/``scp`` into a micro-VM through SshRelay, against the real cluster.

The keypair is generated inside the sandbox by its own ssh-keygen (the sandbox's sshd rejects
ed25519 blobs produced by this host's cryptography lib), pulled out once for the local ssh client.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, "/opt/data/cache/scratch/kube-sandbox-dev")
from kubesandbox import SandboxClient, SshRelay, ensure_sshd, read_client_key, resolve_kubeconfig

sc = SandboxClient(resolve_kubeconfig("/opt/sbx/kubeconfig"))
claim = sc.create("t-relay-e2e")
print("claim:", claim.name, "pod:", claim.sandbox)

public_key = ensure_sshd(sc.client, claim)
print("authorized:", public_key[:40], "…")
private = read_client_key(sc.client, claim)
keyfile = tempfile.NamedTemporaryFile("wb", suffix=".key", delete=False)
keyfile.write(private)
keyfile.close()
os.chmod(keyfile.name, 0o600)

common = ["-i", keyfile.name, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
          "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]
with SshRelay(sc.client, claim) as relay:
    print("relay on port", relay.port)
    proc = subprocess.run(["ssh", *common, "-p", str(relay.port), "root@127.0.0.1",
                           "echo SSH-RELAY-OK; uname -n; id -un; pwd"], capture_output=True, timeout=90)
    print("ssh rc:", proc.returncode)
    print("stdout:", proc.stdout.decode()[:200])
    if proc.returncode != 0:
        print("stderr:", proc.stderr.decode()[-300:])

with SshRelay(sc.client, claim) as relay:
    local = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    local.write("relay-content\n")
    local.close()
    proc = subprocess.run(["scp", *common, "-P", str(relay.port), local.name,
                           "root@127.0.0.1:/tmp/relay-upload.txt"], capture_output=True, timeout=90)
    print("scp rc:", proc.returncode, proc.stderr.decode()[-150:])
    out, rc = sc.exec(claim, ["bash", "-c", "cat /tmp/relay-upload.txt"])
    print("file in sandbox:", rc, out.decode().strip())

os.unlink(keyfile.name)
sc.remove(claim.name)
print("claim removed:", claim.name)
