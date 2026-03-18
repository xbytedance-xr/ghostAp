
with open("src/main.py", "r") as f:
    content = f.read()

imports_add = """from .utils.sys_monitor import start_monitor
"""

if "from .utils.sys_monitor" not in content:
    content = content.replace("from .config import get_settings", imports_add + "from .config import get_settings")

start_add = """    # Start system monitor (log every 5 minutes)
    start_monitor(interval=300)
    
"""

if "start_monitor" not in content:
    content = content.replace("    client.start()", start_add + "    client.start()")

with open("src/main.py", "w") as f:
    f.write(content)
