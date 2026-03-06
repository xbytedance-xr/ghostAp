import sys
import subprocess
import threading
import shutil

def forward_stdin(proc):
    """Forward wrapper's stdin to subprocess's stdin."""
    try:
        while True:
            # Blocking read is fine in a thread
            chunk = sys.stdin.buffer.read(4096)
            if not chunk:
                break
            proc.stdin.write(chunk)
            proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except:
            pass

def forward_stdout(proc):
    """Forward subprocess's stdout to wrapper's stdout, stripping banner."""
    json_started = False
    try:
        while True:
            # Before JSON starts, read line-by-line to filter
            if not json_started:
                line = proc.stdout.readline()
                if not line:
                    break
                
                # Check for JSON start (ACP messages start with '{')
                if line.strip().startswith(b'{'):
                    json_started = True
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
                else:
                    # Ignore banner lines
                    # Debug: print filtered lines to stderr if needed
                    # sys.stderr.write(f"[Wrapper filtered] {line.decode('utf-8', 'ignore')}")
                    continue
            else:
                # After JSON starts, just pipe chunks
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        pass

def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: ttadk_wrapper.py <command> [args...]\n")
        sys.exit(1)

    cmd = sys.argv[1:]
    
    # Resolve executable path if it's just a name
    if os.path.sep not in cmd[0]:
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd[0] = resolved

    try:
        # Start the actual process
        # bufsize=0 -> Unbuffered to minimize latency
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr, # Pass stderr through directly
            bufsize=0
        )
    except Exception as e:
        sys.stderr.write(f"Failed to start subprocess {cmd}: {e}\n")
        sys.exit(1)

    # Start forwarding threads
    t_in = threading.Thread(target=forward_stdin, args=(proc,), daemon=True)
    t_out = threading.Thread(target=forward_stdout, args=(proc,), daemon=True)
    
    t_in.start()
    t_out.start()
    
    # Wait for process to exit
    try:
        proc.wait()
    except KeyboardInterrupt:
        # Pass signal to child
        proc.terminate()
        proc.wait()
    
    sys.exit(proc.returncode)

import os
if __name__ == "__main__":
    main()
