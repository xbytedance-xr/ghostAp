
def fix_indent(file_path):
    with open(file_path, "r") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        if "import gc; gc.collect()" in line:
            # Check indentation of previous line
            prev_line = lines[i-1]
            spaces = len(prev_line) - len(prev_line.lstrip())
            lines[i] = " " * spaces + "import gc; gc.collect()\n"

    with open(file_path, "w") as f:
        f.writelines(lines)

fix_indent("src/loop_engine/engine.py")
fix_indent("src/deep_engine/engine.py")
