# novelWriter

来源：[`sources.json#novelwriter`](../sources.json)，commit `dc483f9c881dadbc847b90ccf8ecf67658dc30e2`。`pyproject.toml` 声明 `GPL-3.0-or-later AND Apache-2.0 AND ISC AND MIT`；使用或分发依赖前必须分别核对相关许可证。

## 本地写作/结构模型

- 官方 README 将作品描述为由多个小文档组成的 plain-text novel，文档使用轻量标记存正文与元数据；项目文件适合 VCS 和普通文件同步。
- [`docs/source/technical/storage.rst`](../../opensource-downloads/novelWriter/docs/source/technical/storage.rst) 描述项目文件保存文档树与元信息，正文文档作为 UTF-8 文件单独写入。
- 同一资料描述 project index 为可重建 JSON；失效时从正文重建，而不是把重建缓存作为正文的唯一事实来源。
- [`docs/source/usage/organising_project.rst`](../../opensource-downloads/novelWriter/docs/source/usage/organising_project.rst) 通过 Novel、Plot、Characters、Locations、Timeline、Objects、Archive 等明确文件夹类型分开正文和笔记。
- [`docs/source/usage/tags_and_references.rst`](../../opensource-downloads/novelWriter/docs/source/usage/tags_and_references.rst) 以唯一 tag 身份和跨文档 reference 导航人物/剧情；小说层级可从文档内标题解析。
- `novelwriter/core/index.py` 构造标题/tag/reference 查询索引；官方存储文档指出该索引可从文本文件重新生成，与正文存储分离。

## 本项目学习成果

- 保持真实正文/导出的 Markdown/TXT 可被作者直接阅读，不要求未来一定登录本地应用才找得到文本。
- 章节、场景和角色引用使用稳定内部 ID，显示标题可自由修改；检索/导航投影损坏时能重建。
- 作者可以按章节查看质量发现、参考和修改建议；结构化 tag/reference 可成为以后作者资料浏览器的连接方式。

## 不照搬

纯文本目录易同步，但不能承载本项目所有并发、因果账本和封卷 version semantics。维持 SQLite 内的权威状态，以稳定文本导出、增量备份和可重建索引提供可移植性。
