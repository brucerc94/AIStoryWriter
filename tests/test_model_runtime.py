from engine.model_runtime import (
    ModelRuntimeProfile,
    get_model_runtime_profile,
    normalize_model_path,
    set_model_mtp_path,
)


class DummySettings:
    def __init__(self):
        self.model_runtime_profiles = {}


def test_empty_profile_uses_normal_runtime():
    settings = DummySettings()
    profile = get_model_runtime_profile(settings, r"G:\Models\Gemma.gguf")
    assert isinstance(profile, ModelRuntimeProfile)
    assert profile.mtp_model_path == ""
    assert profile.uses_mtp is False


def test_profile_is_stored_once_per_target_model():
    settings = DummySettings()
    model = r"G:\Models\Gemma.gguf"
    mtp = r"G:\Models\mtp-gemma-4-12B-it.gguf"

    set_model_mtp_path(settings, model, mtp)

    assert len(settings.model_runtime_profiles) == 1
    profile = get_model_runtime_profile(settings, model)
    assert profile.mtp_model_path == mtp
    assert profile.uses_mtp is True


def test_equivalent_model_paths_share_one_profile():
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


def test_model_path_key_is_absolute_and_normalized():
    value = normalize_model_path(r"G:\Models\Gemma.gguf")
    assert value.endswith(r"models\gemma.gguf")
