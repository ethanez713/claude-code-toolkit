import unittest

from pkill_guard_hook import self_matching

SELF_MATCHING = [
    "pkill -f gmake; free -m",
    "kill 82656 2>/dev/null; pkill -f gmake",
    "pkill -9 -f 'qemu-system.*-avd smoke'",
    "pkill -f \"until grep -q\" ; pkill -f replay_takes.sh",
    "pgrep -f 'llama.py --build' | xargs -r kill; cd /tmp",
    "kill $(pgrep -f bin/myapp)",
    "adb -s X shell 'touch stop; pkill -f contend.sh; sleep 1'",
]

HARMLESS = [
    "pkill -f '[g]make'",
    "pgrep -f '[l]lama.py' | xargs -r kill",
    "pkill cc1plus",
    "until ! pgrep -f memprobe.sh >/dev/null; do true; done",
    "pgrep -fa gradle",
    "git commit -m 'stop using pkill -f in the runner'",
    "cat > kill.sh <<'EOF'\npkill -f na_app.py\nEOF\necho written",
    "git commit -m \"Refuse self-kills\n\npkill -f and pgrep -f match their own shell\"",
    "commit \"Refuse self-kills\n\npkill -f and pgrep -f match their own shell\" settings.json",
    "impact.py add --hypothesis \"pkill -f matches its own shell\"",
]


class PkillGuard(unittest.TestCase):
    def test_given_a_kill_matching_its_own_shell_when_checked_then_refused(self):
        for command in SELF_MATCHING:
            with self.subTest(command=command):
                self.assertTrue(self_matching(command))

    def test_given_a_harmless_command_when_checked_then_allowed(self):
        for command in HARMLESS:
            with self.subTest(command=command):
                self.assertFalse(self_matching(command))


if __name__ == "__main__":
    unittest.main()
