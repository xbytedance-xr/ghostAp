
with open("src/loop_engine/engine.py", "r") as f:
    content = f.read()

# First, remove the previously added save_state
bad_save = """                    
                # Save state precisely after each iteration finishes
                try:
                    self.save_state()
                except Exception as e:
                    logger.warning("[Loop:%s] 细粒度状态保存失败: %s", project_name, e)"""

content = content.replace(bad_save, "")

# Now add it right after callbacks.on_iteration_done
on_done = """                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)"""

on_done_repl = """                if callbacks.on_iteration_done:
                    callbacks.on_iteration_done(iteration, record)
                    
                # Save state precisely after each iteration finishes to support fine-grained recovery
                try:
                    self.save_state()
                except Exception as e:
                    logger.warning("[Loop:%s] 细粒度状态保存失败: %s", project_name, e)"""

content = content.replace(on_done, on_done_repl)

with open("src/loop_engine/engine.py", "w") as f:
    f.write(content)
