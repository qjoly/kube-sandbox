# kube-sandbox

Run every command a Hermes agent issues inside a **gVisor micro-VM claimed from the cluster's sandbox
warm pool** — the terminal tool, `execute_code` and the file tools alike — and destroy it when the
session ends.

Dispatch is structural, not a prompt rule: Hermes builds its shell environment through
`TerminalEnvironmentProvider.create_environment()`, and this backend resolves that to a `kube-sandbox`
environment. Nothing the model writes has another route to the host.

```
kubesandbox/            shared library (no Hermes import): kubeconfig, REST, exec stream, claim lifecycle
plugin/                 the Hermes plugin (__init__.py + plugin.yaml) — copy to $HERMES_HOME/plugins/kube-sandbox/
tests/                  offline tests (stubbed sockets / stubbed cluster)
tools/e2e_terminal.py   end-to-end check against a real cluster, through the real terminal tool
```

## How it runs a command

1. `create_environment(task_id=…)` builds a lazy environment; the **first** command creates a
   `SandboxClaim` named after the task (`AlreadyExists` → reuse), which the agent-sandbox controller
   answers with a pod from the warm pool. Later commands reuse it.
2. Each command goes through a WebSocket **exec** stream (`v4.channel.k8s.io`) — no `kubectl` in the
   path, no shell that outlives the call, a real exit code taken from the error channel.
3. `cleanup()` deletes the claim (idempotent; an already-gone claim is success). A failed delete
   raises, so Hermes keeps treating background processes as possibly alive.

Configuration (`hermes config set …`):

| key | default | meaning |
|---|---|---|
| `terminal.backend` | — | `kube-sandbox` |
| `terminal.kubeconfig` | in-cluster token | kubeconfig path (a token-based one: an `exec` login plugin cannot run here) |
| `terminal.kube_sandbox_namespace` | `sandboxes` | namespace of the claim |
| `terminal.kube_sandbox_template` | `dev` | `SandboxTemplate` / warm pool to claim from |
| `terminal.kube_sandbox_container` | `dev` | container inside the pod |
| `terminal.kube_sandbox_home` | `/root` | starting directory, also taken offline into the prompt |
| `terminal.kube_sandbox_start_timeout` | `180` | seconds to wait for a ready claim |

Requires Hermes ≥ 0.22.5. `KUBECONFIG`, `GITOPS_TOKEN` and `SBX_KUBECONFIG` are stripped from every
process the agent spawns (and never reach the sandbox).

## Known limit: background processes report no exit code

`terminal(background=true)` starts the job and its output arrives, but the core's poller reads its
liveness with `kill -0 <pid>`: inside a micro-VM the exec shell is not PID 1, so the finished job becomes
a **zombie** and `kill -0` keeps succeeding — the session stays "running" and the exit code is never
collected. That is a property of the shared `BaseEnvironment` wrapper (written for a host where the
spawned shell is a normal process), not of this backend, so the plugin does not paper over it.

Practical rule: treat `background=true` here as "fire and read the log with follow-up commands";
`cleanup()` still destroys the VM and everything in it. This is why the plugin sets
`container_persistent: false` semantics regardless of the setting (a claim is always destroyed on
cleanup, no snapshot is taken).

## Tests

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q      # plugin needs hermes_yaml, psutil, requests
PYTHONPATH=$PWD:/path/to/hermes-agent python tools/e2e_terminal.py   # real cluster, real terminal tool
```

The end-to-end run reports, per check: commands run in the sandbox (hostname differs from the
Hermes host), no Hermes credential visible inside it, and a background process whose exit code is
collected by the core poller.

## SSH into a sandbox (Phase 0 transport)

`kubesandbox.ssh_relay` runs an sshd inside the sandbox and exposes it on a **local TCP port**:

```python
from kubesandbox import SandboxClient, SshRelay, ensure_sshd, read_client_key, resolve_kubeconfig
sc = SandboxClient(resolve_kubeconfig(...))
claim = sc.create("my-task")
ensure_sshd(sc.client, claim)                 # installs openssh-server, starts sshd on 127.0.0.1:22
key = read_client_key(sc.client, claim)        # the sandbox-generated keypair, for the local client
with SshRelay(sc.client, claim) as relay:     # local listener -> exec stream -> nc 127.0.0.1 22
    ssh -i key -p relay.port root@127.0.0.1 '…'   # plain OpenSSH, no ProxyCommand needed
```

Verified end-to-end on mocha (`tools/e2e_ssh_relay.py`): `ssh` and `scp` from the host land in the
micro-VM. The keypair is generated **inside** the sandbox by its own `ssh-keygen`, because the sandbox's
sshd refuses ed25519 public keys produced by this host's `cryptography` build. Pointing Hermes' own
`ssh` backend at the relay (`terminal.ssh_host=127.0.0.1`, `ssh_port=<relay.port>`,
`ssh_user=root`, the key above) is the Phase 0 wiring; it is deliberately not part of the plugin.

## Not done yet

- **Desktop streams** (`supports_streams` / `exec_prefix`) — Phase 3 in the plan, needs an image with the
  desktop stack.
