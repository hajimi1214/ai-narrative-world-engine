const terms: Record<string, string> = {
  DRAFT: "草稿", ACTIVE: "运行中", PAUSED: "已暂停", RUNNING: "运行中",
  CORE_CANON: "核心规则", WORLD_FACT: "世界事实", SECRET_CANON: "秘密事实", TEMPORARY: "临时事实",
  OPEN: "待处理", VALIDATED: "已验证", STALE: "已失效", ADOPTED: "已采用", ABORTED: "已放弃",
  MANUAL_EDIT: "手动修改", AI_REPAIR: "AI 修复", ADOPT: "采用", ABORT: "放弃", EDIT_WORLD: "补充世界事实",
  LOCATION: "地点", CITY: "地点", COUNTRY: "国家", SECT: "组织", FACTION: "势力", ITEM: "物品", SYSTEM: "系统", HISTORY: "历史", CUSTOM: "其他",
  KNOWN: "已确认", SUSPECTED: "怀疑", FALSE_BELIEF: "错误认知", VALID: "有效", REJECTED: "已拒绝", UNRESOLVED: "待裁定",
};

export function term(value?: string | null) { return value ? (terms[value] || value) : "-"; }
export function displayStatus(value?: string | null) { return term(value); }
