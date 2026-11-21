# BookStack

来源：[`sources.json#bookstack`](../sources.json)，commit `34edd552294e4ba0603e55542ae5fafef28b96e7`。其 README 和 Composer metadata 声明 MIT。

## 页面历史的模型观察

- `app/Entities/Models/PageRevision.php` 将 revision 作为独立实体链接到 page，并保存作者与旧正文表示。
- `app/Entities/Repos/RevisionRepo.php::storeNewForPage` 每次形成 revision 序号、保存内容、生成作者/时间信息，同时存在显式历史保留策略。
- `app/Entities/Controllers/PageRevisionController.php` 恢复前会检验修订来自目标 page，并要求可编辑权限，再把旧内容送回当前页面更新流程。
- `app/Entities/Controllers/PageRevisionController.php` 提供与前一版本的差异比较；指定 revision 恢复时把内容送回 page 更新流程，历史项本身仍然是旧 revision。

## 本项目学习成果

历史差异/恢复的入口应以内容实体本身为中心；当前正文版本应另存为新版本，不要通过“把 historical row active”抹掉操作来源。作者点击恢复之前可先对比，恢复后的 quality 与作者审批重新生成。

## 差异

BookStack 的修订页面主要是文档管理。本项目每次采用章节还会改变正式叙事/世界历史，因此作者文本版本与 formal scene state 需分层记录。纯页面 restore 不能作为后果一致性的实现。
