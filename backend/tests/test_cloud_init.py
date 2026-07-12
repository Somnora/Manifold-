"""cloud-init user-data invariants. The sidecar's dependencies must install
into the SAME interpreter its systemd unit runs, or it crash-loops on boot
and shows 'sidecar not reachable' forever — the recurring live-test symptom.
"""

import re

from app.cloud_init import build_user_data


def test_sidecar_deps_target_the_service_interpreter():
    ud = build_user_data()

    # The service runs the system interpreter explicitly.
    assert "ExecStart=/usr/bin/python3 /opt/manifold/manifold_sidecar.py" in ud

    # Its deps are installed with that SAME interpreter (not a bare `python3`
    # that might resolve to conda on Lambda ML images).
    install = next(
        l for l in ud.splitlines()
        if "pip install" in l and "fastapi" in l
    )
    assert install.startswith("/usr/bin/python3 -m pip install")
    assert "uvicorn" in install and "pynvml" in install

    # PEP 668 handled, with a fallback for older pip, and non-fatal so a
    # dep failure cannot brick an otherwise usable GPU box.
    assert "--break-system-packages" in ud
    assert "sidecar deps failed to install" in ud


def test_no_bare_python3_pip_install_for_sidecar_deps():
    """Guard against regressing to `python3 -m pip install fastapi ...`, which
    is the exact line that put the deps on the wrong interpreter."""
    ud = build_user_data()
    for line in ud.splitlines():
        if "pip install" in line and "fastapi" in line:
            assert not re.match(r"\s*python3 -m pip install", line), line


def test_sidecar_binds_loopback_only():
    ud = build_user_data()
    # Belt-and-suspenders: the embedded sidecar still serves 127.0.0.1 only.
    assert '127.0.0.1", port=9411' in ud
