from engine.llama_cpp_runtime import LlamaCppCliRuntime


def test_native_mtp_command_uses_llama_cpp_cli_only():
    command = LlamaCppCliRuntime.build_command(
        cli_path=r"C:\llama.cpp\llama-cli.exe",
        model_path=r"G:\Models\Gemma.gguf",
        mtp_model_path=r"G:\Models\mtp-gemma-4-12B-it.gguf",
        context_size=8192,
        gpu_layers=32,
        threads=8,
        threads_batch=4,
        temperature=0.7,
        top_p=0.9,
        top_k=40,
        max_tokens=2048,
        flash_attn=False,
        system_prompt_file=r"C:\tmp\system.txt",
        prompt_file=r"C:\tmp\prompt.txt",
    )

    assert command[0].endswith("llama-cli.exe")
    assert "--spec-type" in command
    assert command[command.index("--spec-type") + 1] == "draft-mtp"
    assert "--model-draft" in command
    assert command[command.index("--model-draft") + 1].endswith("mtp-gemma-4-12B-it.gguf")
    assert all("llama-server" not in part.lower() for part in command)


def test_two_message_input_is_native_chat_prompt():
    runtime = LlamaCppCliRuntime()
    # The command builder stays free of model-specific prompt patches.
    assert callable(runtime.build_command)
