
__test__ = False


def update_finally(file_path):
    with open(file_path, "r") as f:
        content = f.read()

    # Find finally: block
    if "finally:" in content:
        # Instead of parsing, let's just append to _run_state = EngineRunState.IDLE
        content = content.replace("self._run_state = EngineRunState.IDLE", "self._run_state = EngineRunState.IDLE\n            import gc; gc.collect()")
        with open(file_path, "w") as f:
            f.write(content)

if __name__ == "__main__":
    update_finally("src/loop_engine/engine.py")
    update_finally("src/deep_engine/engine.py")
