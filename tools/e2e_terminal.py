"""End-to-end: the real terminal tool with ``terminal.backend: kube-sandbox``, against the cluster.

Run with a Hermes interpreter: ``PYTHONPATH=<repo>:$HERMES_SRC HERMES_HOME=<dir> python tools/e2e_terminal.py``.
Prints a JSON report and exits non-zero when a check fails.
"""

import json
import re
import subprocess
import sys
import time


def main() -> int:
    from hermes_cli.plugins import discover_plugins

    discover_plugins()  # what CLI/agent startup does: enabled plugins register their backends
    from agent.terminal_env_registry import plugin_backend_names
    from tools.process_registry import process_registry
    from tools.terminal_tool import terminal_tool

    report = {"registered_backends": plugin_backend_names(), "checks": {}}
    checks = report["checks"]

    def record(name, ok, detail):
        checks[name] = {"ok": bool(ok), "detail": detail}

    report["host_hostname"] = subprocess.run(["uname", "-n"], capture_output=True, text=True).stdout.strip()

    out = terminal_tool(command="uname -n; id -un; pwd")
    record("commands run in the sandbox", "hermes-carolyn" not in out and "root" in out, out)
    # The token file only exists in the pod's own filesystem: what must not be reachable is the
    # HOST's, and no Hermes credential or kubeconfig may be there.
    env_out = terminal_tool(command="env | sort")
    leaked = [name for name in ("KUBECONFIG", "GITOPS_TOKEN", "GH_TOKEN", "SBX_KUBECONFIG")
              if re.search(rf"^{name}=", env_out, re.MULTILINE)]
    record("no Hermes credential in the micro-VM", not leaked, {"leaked": leaked, "env": env_out[:600]})

    # Known limit (README): the core's poller sees a zombie as alive (the exec shell is not PID 1), so a
    # background job's exit code is never collected here. What IS checked: the job ran in the sandbox and
    # its output arrived.
    started = terminal_tool(command="sleep 3; echo bg-done", background=True)
    match = re.search(r'"session_id": "([^"]+)"', started) or re.search(r"\b(proc_[0-9a-f]+)", started)
    sid = match.group(1) if match else ""
    time.sleep(8)
    listed = process_registry.list_sessions()
    session = {(s.get("session_id") if isinstance(s, dict) else s.id): s for s in listed}.get(sid)
    get = (lambda obj, key: obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None))
    poll = ({"status": get(session, "status"), "output": (get(session, "output_preview") or "").strip()[-300:]}
            if session else None)
    record("background process runs and its output arrives", bool(poll) and "bg-done" in (poll["output"] or ""),
           {"start": started, "poll": poll})
    print(json.dumps(report, indent=2, default=str))
    return 0 if all(c["ok"] for c in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
