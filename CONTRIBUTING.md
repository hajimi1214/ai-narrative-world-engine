# 贡献指南

## 开发环境

- Python 3.12
- Node.js 20 LTS
- pnpm 10
- uv 0.11 或兼容版本

在仓库根目录运行 `uv sync --locked --extra test --python 3.12`。在 `apps/web` 运行 `pnpm install --frozen-lockfile`。

## 提交前验证

```powershell
uv run --locked python -m pytest -q apps/api/tests
cd apps/web
pnpm test
pnpm exec tsc --noEmit --incremental false
pnpm build
```

API 测试应使用合成内容和隔离数据库。不要使用作者数据库、真实小说正文、真实模型密钥或未经授权的付费模型调用。任何失败和跳过都要在变更说明中逐项记录。

涉及数据库结构的改动必须包含 Alembic 迁移和针对旧库/新库的验证。涉及运行恢复的改动应覆盖检查点、重复请求、服务重启和失败状态。涉及作者页面的改动应在 320、390、768、1024、1440、1920 CSS 像素下验收响应式布局，并把结果记录在 `logs/usability-qa/`。

## 安全与版权

- 不提交 `.env`、API 密钥、私人小说、数据库或备份文件。
- 新增第三方代码或资料前确认其许可证允许采用，并记录来源。
- 项目源代码采用 MIT 许可证。依赖库继续受其各自许可证约束。
