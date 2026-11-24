"""Contract tests for the Hermes environment object, against a stubbed cluster client."""

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugin" / "__init__.py"


class FakeClient:
    def __init__(self):
        self.execs = []   # (claim, command string)
        self.raw = []     # (claim, argv, stdin)
        self.removed = []
        self.replies = {}

    def create(self, name):
        from kubesandbox.sandbox import Claim
        return Claim("sandboxes", name, f"pod-{name}", "dev")

    def exec(self, claim, command, timeout=120, stdin_data=None):
        script = command[-1] if len(command) == 3 and command[:2] == ["bash", "-c"] else " ".join(command)
        self.raw.append((claim, command, stdin_data))
        self.execs.append((claim, script))
        for needle, reply in self.replies.items():
            if needle in script:
                return reply
        return b"", 0

    def remove(self, name):
        self.removed.append(name)
        return True


@pytest.fixture
def env_mod():
    spec = importlib.util.spec_from_file_location("kube_sandbox_plugin_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_env(env_mod):
    client = FakeClient()
    env = env_mod.KubeSandboxEnvironment(cwd="/root", timeout=30, task_id="task-1",
                                         client_factory=lambda: client)
    return env, client


def test_execute_returns_the_base_result_with_the_command_status(fake_env):
    # BaseEnvironment.execute() is the contract the core consumes: the merged output and the shell's
    # own status off the end (no wrapper artifacts left in the text).
    env, client = fake_env
    client.replies["echo hi"] = (b"hi\n", 3)
    out = env.execute("echo hi")
    assert out["output"] == "hi\n" and out["returncode"] == 3


def test_a_timed_out_command_is_reported_as_124(fake_env):
    env, client = fake_env
    client.replies["sleep"] = (b"partial\n\n[Command timed out after 5s]\n", 124)
    out = env.execute("sleep 99", timeout=5)
    assert out["returncode"] == 124 and "partial" in out["output"]
    assert "timed out" in out["output"]


def test_execute_wraps_in_the_session_cwd(fake_env):
    env, client = fake_env
    env.execute("ls")
    assert "cd -- /root" in client.execs[-1][1]


def test_claim_is_created_once_and_reused(fake_env):
    env, client = fake_env
    env.execute("true")
    env.execute("true")
    assert [c for c, _ in client.execs][0].name == "task-1"
    assert len({id(c) for c, _ in client.execs}) == 1


def test_cleanup_removes_the_claim_and_is_idempotent(fake_env):
    env, client = fake_env
    env.execute("true")
    env.cleanup()
    env.cleanup()
    assert client.removed == ["task-1"]


def test_cleanup_before_any_command_touches_nothing(fake_env):
    env, client = fake_env
    env.cleanup()
    assert client.removed == [] and client.execs == []


def test_cleanup_failure_raises_so_the_sandbox_is_not_declared_dead(env_mod, fake_env):
    env, client = fake_env
    env.execute("true")

    def boom(name):
        raise RuntimeError("api down")
    client.remove = boom
    with pytest.raises(RuntimeError, match="may still be alive"):
        env.cleanup()


def test_background_commands_ride_the_core_poller(fake_env):
    # Background work is the core's ("nohup ... & echo $!" wrapped by process_registry.spawn_via_env),
    # so the plugin must not own a second implementation: the environment only has to run it.
    env, client = fake_env
    client.replies["nohup"] = (b"4242\n", 0)
    out = env.execute("( nohup bash -lc 'sleep 1' > /tmp/x.log 2>&1 ) & echo $!")
    assert out["returncode"] == 0 and "4242" in out["output"]


def test_provider_flags_match_the_plan(env_mod):
    p = env_mod.KubeSandboxProvider()
    assert p.name == "kube-sandbox"
    assert p.is_remote and p.is_container
    assert p.skip_container_guards is False            # dangerous-command approvals stay on
    assert p.session_isolated_when_nonpersistent is True
    assert "KUBECONFIG" in p.strip_env_keys and "GITOPS_TOKEN" in p.strip_env_keys
    assert p.default_cwd.startswith("/")


def test_create_environment_ignores_unknown_kwargs_and_default_images(env_mod, monkeypatch):
    seen = {}

    def factory():
        c = FakeClient()
        seen["client"] = c
        return c

    monkeypatch.setattr(env_mod, "_sandbox_client", factory)
    p = env_mod.KubeSandboxProvider()
    env = p.create_environment(cwd="~", timeout=10, task_id="t", image="", container_config={},
                               scope=None, sandbox="x", brand_new_key=1)
    assert env.cwd == p.default_cwd
    env.cleanup()


def test_is_available_and_probe_never_touch_the_network(env_mod, monkeypatch):
    p = env_mod.KubeSandboxProvider()
    monkeypatch.setattr(env_mod, "_cfg", lambda *a, default=None, **k: default)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(env_mod.os.path, "exists", lambda path: "serviceaccount" in str(path))
    assert p.is_available() is True         # in-cluster token: a filesystem check, nothing more
    assert p.probe() == ("ready", "")
    monkeypatch.setattr(env_mod.os.path, "exists", lambda path: False)
    assert p.is_available() is False and p.probe()[0] == "needs_setup"
