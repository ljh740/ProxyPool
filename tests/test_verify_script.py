import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "scripts" / "verify.sh"

FAKE_DOCKER_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    STATE_PATH = os.environ["FAKE_DOCKER_STATE"]


    def load_state():
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)


    def save_state(state):
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(state, handle)


    def count_entries(raw):
        if raw in (None, ""):
            return 0
        return len(json.loads(raw))


    def build_seed_raw():
        entries = []
        for offset in range(5):
            port = 10001 + offset
            host = f"verify-{offset + 1}.example.com"
            entries.append(
                {
                    "key": f"seed-{offset}",
                    "label": f"{host}:{port}",
                    "hops": [
                        {
                            "scheme": "http",
                            "host": host,
                            "port": port,
                            "username": "",
                            "password": "",
                        }
                    ],
                    "source_tag": "verify",
                    "in_random_pool": True,
                }
            )
        return json.dumps(entries)


    def handle_exec(args):
        state = load_state()
        if args[5] == "/opt/helper/router.py":
            upstream_count = max(count_entries(state["proxy_list"]), 1)
            users = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
            for user in users:
                port = 10001 + (sum(ord(ch) for ch in user) % upstream_count)
                print(f"OK code=200 message={port}")
            return 0
        if args[5] == "-":
            script = sys.stdin.read()
            if '"present": raw is not None' in script:
                raw = state["proxy_list"]
                print(json.dumps({"present": raw is not None, "raw": raw or ""}))
                return 0
            if "save_proxy_list(open_storage(), entries)" in script and "verify-" in script:
                state["proxy_list"] = build_seed_raw()
                state["seed_calls"] += 1
                save_state(state)
                print("5")
                return 0
            if '"upstream_count": len(entries)' in script:
                print(
                    json.dumps(
                        {
                            "routing": "shared",
                            "upstream_source": "admin",
                            "upstream_count": count_entries(state["proxy_list"]),
                        }
                    )
                )
                return 0
            if "snapshot_path = sys.argv[1]" in script:
                state["unexpected_restore_path_args"].append(args[6:])
                save_state(state)
                return 1
            raise SystemExit(f"unsupported python-stdin exec: {args}")
        if args[5] == "-c":
            if os.environ.get("FAKE_DOCKER_RESTORE_FAIL") == "1":
                return 1
            snapshot = json.load(sys.stdin)
            state["restore_calls"] += 1
            if snapshot.get("present"):
                state["proxy_list"] = snapshot["raw"]
            else:
                state["proxy_list"] = None
            save_state(state)
            return 0
        raise SystemExit(f"unsupported compose exec args: {args}")


    def main():
        args = sys.argv[1:]
        if args[:3] == ["compose", "ps", "-q"] and args[3:] == ["squid"]:
            print("fake-squid-id")
            return 0
        if args[:5] == ["compose", "exec", "-T", "squid", "python3"]:
            return handle_exec(args)
        raise SystemExit(f"unsupported docker args: {args}")


    if __name__ == "__main__":
        raise SystemExit(main())
    """
)


class TestVerifyScript(unittest.TestCase):
    def _make_state_file(self, directory):
        path = Path(directory) / "docker-state.json"
        path.write_text(
            json.dumps(
                {
                    "proxy_list": None,
                    "seed_calls": 0,
                    "restore_calls": 0,
                    "unexpected_restore_path_args": [],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _make_fake_docker(self, directory):
        path = Path(directory) / "docker"
        path.write_text(FAKE_DOCKER_SCRIPT, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _run_verify(self, *, restore_fail=False):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = self._make_state_file(tmp_dir)
            self._make_fake_docker(tmp_dir)
            env = os.environ.copy()
            env["PATH"] = f"{tmp_dir}:{env['PATH']}"
            env["FAKE_DOCKER_STATE"] = str(state_path)
            env["SAMPLE_USERS"] = "3"
            if restore_fail:
                env["FAKE_DOCKER_RESTORE_FAIL"] = "1"
            result = subprocess.run(
                [str(VERIFY_SCRIPT)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
        return result, state

    def test_verify_script_restores_seeded_proxy_list(self):
        result, state = self._run_verify()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("seeded temporary upstreams: 5", result.stdout)
        self.assertIn("ok", result.stdout)
        self.assertEqual(state["seed_calls"], 1)
        self.assertEqual(state["restore_calls"], 1)
        self.assertIsNone(state["proxy_list"])
        self.assertEqual(state["unexpected_restore_path_args"], [])

    def test_verify_script_fails_when_snapshot_restore_fails(self):
        result, state = self._run_verify(restore_fail=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to restore proxy list snapshot", result.stderr)
        self.assertEqual(state["seed_calls"], 1)
        self.assertEqual(state["restore_calls"], 0)
        self.assertIsNotNone(state["proxy_list"])
