
with open("src/loop_engine/engine.py", "r") as f:
    content = f.read()

convergence_find = """                # Convergence detection
                if self._detect_convergence():
                    logger.info("[Loop:%s] 收敛检测触发, 迭代 %d 轮", project_name, iteration)
                    break"""
convergence_repl = """                # Convergence detection
                if self._detect_convergence():
                    logger.info("[Loop:%s] 收敛检测触发, 迭代 %d 轮", project_name, iteration)
                    break
                    
                # Save state precisely after each iteration finishes
                try:
                    self.save_state()
                except Exception as e:
                    logger.warning("[Loop:%s] 细粒度状态保存失败: %s", project_name, e)"""

content = content.replace(convergence_find, convergence_repl)

with open("src/loop_engine/engine.py", "w") as f:
    f.write(content)
