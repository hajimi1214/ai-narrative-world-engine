"use client";

import { useQuery } from "@tanstack/react-query";
import { Landmark } from "lucide-react";
import { api } from "../../app/lib";
import { term } from "../../app/terminology";
import { DeveloperData, ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../ui/primitives";

export function EntityDetail({ entityId }: { entityId: string }) {
  const query = useQuery({ queryKey:["entity", entityId], queryFn:() => api(`/world-entities/${entityId}`) as Promise<any> });
  if (query.isLoading) return <LoadingState />;
  if (query.isError) return <ErrorState message="暂时无法读取世界实体。" retry={() => void query.refetch()} />;
  const item = query.data;
  return <main className="stack"><PageHeader title={item.name} description={`${term(item.entity_type)} · 世界百科`} action={<StatusBadge value={item.active === false ? "已停用" : "可用"} />} /><SectionCard title="实体资料"><div className="entity-profile"><Landmark size={26} /><div className="key-value-grid">{Object.entries(item.profile || {}).map(([key,value]) => <div key={key}><small>{key}</small><span>{typeof value === "object" ? "已记录" : String(value)}</span></div>)}</div></div><p className="muted">创建时间：{item.created_at || "未记录"}</p></SectionCard><DeveloperData value={item} label="实体原始数据" /></main>;
}
