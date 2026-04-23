#!/bin/bash
# fix-commit-message.sh: Script to help fix commit messages to include all changed files

if [ $# -eq 0 ]; then
    echo "使用方法: $0 <commit-hash>"
    echo "或: $0 (默认修复最新提交)"
    exit 1
fi

if [ $# -eq 1 ]; then
    COMMIT="$1"
else
    COMMIT="HEAD"
fi

echo "=== 分析提交 $COMMIT ==="
echo ""

# Get the commit message
COMMIT_MSG=$(git log --format=%B -n 1 "$COMMIT")

# Get the changed files
CHANGED_FILES=$(git show --name-only --format="" "$COMMIT")

echo "当前提交信息:"
echo "------------------------"
echo "$COMMIT_MSG"
echo "------------------------"
echo ""
echo "修改的文件:"
echo "------------------------"
echo "$CHANGED_FILES"
echo "------------------------"
echo ""

# Prompt to edit the commit message
read -p "是否要修改此提交的提交信息？(y/n): " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    git commit --amend
    echo "提交信息已更新！"
else
    echo "已取消。"
fi
