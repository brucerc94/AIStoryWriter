from engine.model_runtime import (
    ModelRuntimeProfile,
    get_model_runtime_profile,
    normalize_model_path,
    set_model_mtp_path,
)


class DummySettings:
    def __init__(self):
        self.model_runtime_profiles = {}


def test_profile_empty_means_normal_runtime():
    settings = DummySettings()
    profile = get_model_runtime_profile(settings, r"G:\Models\Gemma.gguf")
    assert isinstance(profile, ModelRuntimeProfile)
    assert profile.mtp_model_path == ""
    assert profile.uses_mtp is False


def test_profile_is_stored_once_per_model_path():
    settings = DummySettings()
    model = r"G:\Models\Gemma.gguf"
    mtp = r"G:\Models\mtp-gemma.gguf"

    set_model_mtp_path(settings, model, mtp)

    assert len(settings.model_runtime_profiles) == 1
    profile = get_model_runtime_profile(settings, model)
    assert profile.mtp_model_path == mtp
    assert profile.uses_mtp is True


def test_profile_path_lookup_is_case_insensitive_and_absolute():
    settings = DummySettings()
    model = r"G:\Models\Gemma.gguf"
    mtp = r"G:\Models\mtp-gemma.gguf"

    set_model_mtp_path(settings, model, mtp)

    equivalent = r"g:\models\GEMMA.GGUF"
    assert get_model_runtime_profile(settings, equivalent).mtp_model_path == mtp


def test_empty_mtp_removes_profile():
    settings = DummySettings()
    model = r"G:\Models\Gemma.gguf"
    set_model_mtp_path(settings, model, r"G:\Models\mtp-gemma.gguf")
    set_model_mtp_path(settings, model, "")

    assert get_model_runtime_profile(settings, model).uses_mtp is False
    assert settings.model_runtime_profiles == {}


def test_normalize_model_path_is_stable():
    value = normalize_model_path(r"G:\Models\Gemma.gguf")
    assert value.endswith("models\gemma.gguf")
