"""Make real-model skips loud: a green run that skipped the MLX generation
tests is not evidence those paths work."""

import os
from pathlib import Path


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    skipped = terminalreporter.stats.get("skipped", [])
    reasons = sorted({str(report.longrepr[2]) if isinstance(report.longrepr, tuple) and len(report.longrepr) > 2
                      else "real-model fixture unavailable" for report in skipped})
    if skipped:
        terminalreporter.write_sep(
            "=", f"WARNING: {len(skipped)} test(s) skipped ({'; '.join(reasons)}). "
               "Set BRIDLE_TEST_MODELS_DIR with the fixture models to run the real-model suite.",
            yellow=True)


# ---- persistent fixture cache -------------------------------------------------
# The real-model tests need fixture models (~400 MB total). They are fetched once
# into a stable cache that survives reboot; after that every pytest run reuses
# them with no network. BRIDLE_TEST_MODELS_DIR always wins if set explicitly;
# BRIDLE_SKIP_MODEL_FETCH=1 keeps the old skip-instead-of-download behavior.

FIXTURE_MODELS = ("mlx-community/Qwen2.5-0.5B-Instruct-4bit",
                  "mlx-community/all-MiniLM-L6-v2-bf16")


def _have_all(models_dir):
    from pathlib import Path as _P
    return all((_P(models_dir) / repo / ".bridle.json").exists() for repo in FIXTURE_MODELS)


def _ensure_fixture_models():
    if os.environ.get("BRIDLE_TEST_MODELS_DIR") or os.environ.get("BRIDLE_SKIP_MODEL_FETCH"):
        return
    models_dir = Path.home() / "Library" / "Caches" / "com.bridle.app" / "test-models" / "models"
    if _have_all(models_dir):
        os.environ["BRIDLE_TEST_MODELS_DIR"] = str(models_dir)
        return
    script = Path(__file__).resolve().parents[2] / "scripts" / "fetch_test_models.sh"
    if not script.is_file():
        return
    import subprocess
    try:
        subprocess.run([str(script)], check=True, timeout=900,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return  # offline or failed: the real-model tests skip loudly, as before
    if _have_all(models_dir):
        os.environ["BRIDLE_TEST_MODELS_DIR"] = str(models_dir)


_ensure_fixture_models()
