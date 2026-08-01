from pare.config import PAREConfig  # the dataclass load_config populates


def _specs_after_gate(specs, enable_mitm):
    # mirrors the filter in Agent.setup()
    if not enable_mitm:
        specs = [s for s in specs if s.name != "mitm"]
    return specs


class _Spec:
    def __init__(self, name):
        self.name = name


def test_mitm_excluded_when_disabled():
    specs = [_Spec("frida"), _Spec("static"), _Spec("mitm")]
    names = {s.name for s in _specs_after_gate(specs, enable_mitm=False)}
    assert "mitm" not in names and {"frida", "static"} <= names


def test_mitm_included_when_enabled():
    specs = [_Spec("frida"), _Spec("static"), _Spec("mitm")]
    names = {s.name for s in _specs_after_gate(specs, enable_mitm=True)}
    assert "mitm" in names


def test_config_defaults_mitm_off():
    assert PAREConfig().enable_mitm is False
