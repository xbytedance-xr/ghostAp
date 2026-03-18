
with open("src/spec_engine/engine.py", "r") as f:
    content = f.read()

execute_finally_str = """        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE
            if (
                self._project
                and self._project.status == SpecProjectStatus.COMPLETED
                and self._saved_task_id
            ):
                delete_task_state(self._saved_task_id)
                self._saved_task_id = None"""

execute_finally_repl = """        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE
            if (
                self._project
                and self._project.status == SpecProjectStatus.COMPLETED
                and self._saved_task_id
            ):
                delete_task_state(self._saved_task_id)
                self._saved_task_id = None
                
            # Explicit memory cleanup to prevent leaks under high load
            import gc
            gc.collect()"""

content = content.replace(execute_finally_str, execute_finally_repl)

resume_finally_str = """        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE"""
resume_finally_repl = """        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE
            
            import gc
            gc.collect()"""
content = content.replace(resume_finally_str, resume_finally_repl)

with open("src/spec_engine/engine.py", "w") as f:
    f.write(content)
