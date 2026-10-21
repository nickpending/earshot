import os
import subprocess
import sys

PROBE = (
    "import earshotd\n"
    "from pyannote.audio.telemetry.metrics import is_metrics_enabled\n"
    "import os\n"
    "import huggingface_hub.constants as hub\n"
    # onnxruntime has no Python getter; its native telemetry reads this variable.
    "print(is_metrics_enabled(), hub.HF_HUB_DISABLE_TELEMETRY, os.environ['ORT_DISABLE_TELEMETRY'])\n"
)


def _probe(**env_extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "PYANNOTE_METRICS_ENABLED",
            "HF_HUB_DISABLE_TELEMETRY",
            "ORT_DISABLE_TELEMETRY",
        )
    }
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_importing_earshotd_turns_pyannote_and_hub_telemetry_off():
    # A fresh process with neither switch set: pyannote's own default is on (its telemetry
    # config.yaml), so this is red unless earshotd sets both before the libraries load.
    assert _probe() == ["False", "True", "1"]


def test_an_operator_who_exports_the_switch_still_opts_in():
    assert _probe(PYANNOTE_METRICS_ENABLED="true")[0] == "True"
