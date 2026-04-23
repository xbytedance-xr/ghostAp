# Git Hooks

本目录包含项目的 Git hooks，用于确保提交质量。

## 配置方法

克隆仓库后，运行以下命令配置 Git 使用这些 hooks：

```bash
git config core.hooksPath .githooks
```

## 包含的 Hooks

- `commit-msg`: 检查提交信息是否提到修改的文件
- `pre-commit`: 显示即将提交的文件并提醒检查提交信息
