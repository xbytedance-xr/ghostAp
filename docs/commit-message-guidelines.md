# 提交信息规范

## 格式规范

提交信息应遵循以下格式：

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Type 类型

- `feat`: 新功能
- `fix`: 修复 bug
- `docs`: 文档更新
- `style`: 代码格式调整（不影响功能）
- `refactor`: 重构
- `perf`: 性能优化
- `test`: 测试相关
- `chore`: 构建/工具链相关

### Subject 主题

- 简短描述，不超过 50 字符
- 使用动词原形开头
- 首字母小写，结尾不加句号

### Body 正文（可选）

- 详细说明变更的内容和原因
- 每行不超过 72 字符
- 解释「为什么」而不是「是什么」

### Footer 页脚（可选）

- 关联的 Issue 或 PR
- 破坏性变更提示

## 内容要求

1. **准确反映变更范围**：提交信息必须明确列出所有被修改的主要模块/文件
2. **示例**：
   ```
   fix: move inline imports to top level in deep_engine and perspective_worker

   - 将 deep_engine/engine.py 中的内联导入移到顶部
   - 将 spec_engine/perspective_worker.py 中的内联导入移到顶部
   - 提升代码可维护性
   ```

3. **避免**：
   - 不要只提部分文件，而实际修改了更多
   - 不要使用模糊的描述（如「update some files」）
   - 不要提交不相关的变更在一起

## 检查清单

提交前请确认：
- [ ] 提交信息清晰描述了所有变更
- [ ] 提及了所有主要修改的模块/文件
- [ ] 提交历史记录可以反映真实的开发过程
