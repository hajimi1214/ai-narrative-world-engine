"""Small deterministic long-form fiction corpus used by baseline regression tests."""
from __future__ import annotations


def build_golden_novel() -> dict:
    characters = [
        {"id": f"char_{name}", "name": label}
        for name, label in (
            ("lin", "林舟"), ("mo", "莫遥"), ("qiao", "乔岚"), ("shen", "沈砚"), ("yu", "余烬"),
            ("he", "何川"), ("lu", "陆沉"), ("yan", "严棠"), ("bai", "白芷"), ("tang", "唐未"),
        )
    ]
    canon = [{"id": f"canon_{index:02d}", "statement": f"世界事实 {index:02d}"} for index in range(1, 26)]
    threads = [{"id": f"thread_{index:02d}", "title": f"剧情线程 {index:02d}"} for index in range(1, 11)]
    arcs = [{"id": f"arc_{index}", "number": index, "chapter_numbers": list(range((index - 1) * 10 + 1, index * 10 + 1))} for index in range(1, 4)]
    chapters = []
    for number in range(1, 31):
        chapters.append({
            "id": f"chapter_{number:03d}", "number": number, "arc_id": f"arc_{(number - 1) // 10 + 1}",
            "pov_character_id": characters[(number - 1) % len(characters)]["id"],
            "thread_ids": [threads[(number - 1) % len(threads)]["id"]],
            "required_canon": [canon[(number - 1) % len(canon)]["id"]],
            "required_events": [f"beat_{((number - 1) * 2) + 1:02d}", f"beat_{((number - 1) * 2) + 2:02d}"],
            "forbidden_events": ["reveal_final_truth"] if number < 28 else [],
            "delivered_events": [f"beat_{((number - 1) * 2) + 1:02d}", f"beat_{((number - 1) * 2) + 2:02d}"],
            "reveals": [],
        })
    timeline = []
    for index in range(1, 21):
        hour = 8 + (index - 1) * 2
        day, hour = divmod(hour, 24)
        timeline.append({
            "id": f"event_{index:02d}", "start_time": f"2041-03-{1 + day:02d}T{hour:02d}:00:00",
            "end_time": f"2041-03-{1 + day:02d}T{hour:02d}:30:00", "location_id": f"loc_{index % 4}",
            "participants": [characters[(index - 1) % len(characters)]["id"]],
        })
    foreshadowings = [{"id": f"foreshadow_{index:02d}", "planted_chapter": index, "payoff_chapter": index + 10, "required": True, "status": "PLANNED"} for index in range(1, 11)]
    knowledge_matrix = {character["id"]: {fact["id"]: ("KNOWN" if fact_index <= 3 else "UNKNOWN") for fact_index, fact in enumerate(canon, 1)} for character in characters}
    return {
        "book": {"id": "golden_mist_harbor", "title": "雾港来信", "chapters": 30},
        "characters": characters, "canon": canon, "threads": threads, "arcs": arcs, "chapters": chapters,
        "timeline": timeline, "foreshadowings": foreshadowings, "knowledge_matrix": knowledge_matrix,
        "style_samples": [
            "雨停在凌晨三点以后。林舟没有关灯，坐在邮局柜台后面，把那封没有寄件人的信翻来覆去地看。纸张边缘有一道很细的折痕，像是有人在犹豫要不要把它寄出去。",
            "莫遥把钥匙放在桌上，没有推过去。林舟看了她一眼，问她是不是早就知道。她说不知道，手指却压住了钥匙齿上的缺口。两个人都没有再问第二遍。",
            "档案馆的窗户朝北，下午没有阳光。乔岚沿着书架往里走，鞋跟每落一次，灰尘就从旧目录上抖下来。她在最底层找到一张空白卡片，背面写着今天的日期。",
            "沈砚站在门外，听见里面有人笑。他本来可以推门进去，手抬到一半却停住了。那笑声里有一个短促的停顿，和他记忆中的那个人一模一样。",
            "信纸最后只剩一行字：别让他在第十二天去港口。林舟读完后把纸折好，塞进外套内袋。他没有告诉任何人，因为他还没想好自己究竟是在保护谁。",
        ],
    }
