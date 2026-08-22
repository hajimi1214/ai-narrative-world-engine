# AI Narrative World Engine

## 作者本地使用

这是单用户本地小说工作台。首次准备好 Python 和 Node.js 后，在项目根目录双击 `start-platform.bat`，或运行：

```powershell
.\start-platform.ps1
```

默认使用项目根目录的 `narrative.db`，不会要求单独安装数据库。打开 [http://127.0.0.1:3000](http://127.0.0.1:3000) 即可开始创作。模型密钥只保存在本机配置中，不会写入小说备份。

进入某部小说后，打开“小说安全”可以：

- 下载整部小说的备份文件；
- 从备份导入一个新的恢复副本，原小说不会被覆盖；
- 记录和查看恢复点。

建议在完成一卷、进行大幅修改或通过章节质量检查后下载一次备份。

## 手动启动（高级）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
$env:DATABASE_URL = "sqlite:///./narrative.db"
alembic -c apps/api/alembic.ini upgrade head
uvicorn app.main:app --app-dir apps/api --reload
```

PostgreSQL 仅适合开发和部署，不是作者本地使用的必要条件。

## 开发者数据库（可选）

```powershell
docker compose up -d postgres
```

## Tests

```powershell
pytest
```
