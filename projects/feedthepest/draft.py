# Feed the Pest -- 群宠：一只养在收容箱里的 Pest。

import random
import re
import time

PREFIX = "!pest"
STATE_VERSION = 2
# 全服共用的存档键：一只 Pest，不是每个频道一只
STATE_KEY = "pest"

# --- 数值 ------------------------------------------------------------------
HUNGER_PER_H = 5.0          # 它永远饿
VIGOR_PER_H = 11.0
ENTROPY_PER_H = 15.0        # 糖分（熵差）会慢慢烧掉
STARVE_BOND_PER_H = 2.0
BOND_CAP = 100.0
VIGOR_BASE = 100
VIGOR_PER_LEVEL = 5         # 每升一级，体力上限 +5
OFFLINE_CAP_H = 12.0        # 离线收益最多累计这么久
IDLE_XP_PER_H = 3.0
SCAVENGE_PER_H = 0.5
AWAY_MIN_H = 0.2            # 离开超过 12 分钟才值得汇报
RENAME_CD = 6 * 3600
FEED_CD = 20
EXPLORE_CD = 60
BAG_KIND_CAP = 14
MAX_FEED_N = 25

# --- 物品 ------------------------------------------------------------------
# kind 类别；nourish 饱食、bond 信任、entropy 跳高加成、xp 经验、tier 稀有度
ITEMS = {
    "bio_scrap": {
        "name": "生物废料", "emoji": "\U0001F9EC", "kind": "resource", "nourish": 0, "bond": -4,
        "xp": 8, "entropy": 0, "tier": 0,
        "description": "一堆生物废料。也许能派上用场。",
        "flavor": "别把它和真正的食物弄混了。",
    },
    "nachos": {
        "name": "玉米片", "emoji": "\U0001F32E", "kind": "food", "nourish": 26, "bond": 4,
        "xp": 4, "entropy": 16, "tier": 0,
        "description": "一些落满灰尘的旧玉米片。",
        "flavor": "很不新鲜，但能吃。",
    },
    "bread": {
        "name": "面包", "emoji": "\U0001F35E", "kind": "food", "nourish": 32, "bond": 3,
        "xp": 5, "entropy": 2, "tier": 0,
        "description": "一块用小麦烤成的香喷喷面包。至少，那东西应该算小麦。",
        "flavor": "",
    },
    "anteverse_wheat": {
        "name": "异世界小麦", "emoji": "\U0001F33E", "kind": "food", "nourish": 20,
        "bond": 2, "xp": 3, "entropy": 1, "tier": 0,
        "description": "来自另一个更加古怪地方的奇异小麦。可能具有治疗作用。",
        "flavor": "把它称作谷物，多少有些勉强。",
    },
    "raw_pest": {
        "name": "害虫生肉", "emoji": "\U0001F969", "kind": "food", "nourish": 34, "bond": -1,
        "xp": 11, "entropy": 6, "tier": 1,
        "description": "未经烹饪的害虫头部。",
        "flavor": "IS-0173",
    },
    "rump": {
        "name": "害虫臀肉", "emoji": "\U0001F356", "kind": "food", "nourish": 40,
        "bond": -2, "xp": 14, "entropy": 9, "tier": 1,
        "description": "未经烹饪的害虫臀肉。",
        "flavor": "一大团肌肉，夹在多得过分的软骨之间。",
    },
    "pheromone": {
        "name": "信息素烧瓶", "emoji": "\U0001F9EA", "kind": "tool", "nourish": 4,
        "bond": 30, "xp": 6, "entropy": 3, "tier": 1,
        "description": "一种投掷后会释放宜人、舒缓气味的混合物，可安抚较低等的 Anteverse 2 生物。",
        "flavor": "",
    },
    "trap": {
        "name": "害虫捕兽夹", "emoji": "\U0001FAA4", "kind": "tool", "nourish": 0, "bond": 12,
        "xp": 8, "entropy": 0, "tier": 1,
        "description": "用于捕捉可驯化生物的可部署陷阱。",
        "flavor": "给困住的害虫喂食特定食物，就能驯服一只带刺宠物。",
    },
}

SCAVENGE_POOL = ("bio_scrap", "bio_scrap", "bio_scrap", "nachos", "bread",
                 "anteverse_wheat", "raw_pest")

# --- 地图 ------------------------------------------------------------------
# min_level 决定这个地方什么时候才去得了。
PLACES = {
    "vent_3b": {
        "name": "维护通风管", "level": 1, "weight": 12, "xp": 6, "danger": 0.18,
        "text": "冷凝水沿管壁往下淌，几只储物箱堵在转角处。",
        "drops": (("bio_scrap", 0.55), ("raw_pest", 0.20)),
    },
    "break_locker": {
        "name": "休息区储物柜", "level": 1, "weight": 8, "xp": 10, "danger": 0.12,
        "text": "柜门没有关严，里面还留着一袋零食。",
        "drops": (("nachos", 0.45), ("bread", 0.30)),
    },
    "cafeteria": {
        "name": "食堂桌下", "level": 1, "weight": 9, "xp": 8, "danger": 0.12,
        "text": "桌脚、空托盘和满地碎屑组成了一条天然掩体。",
        "drops": (("nachos", 0.35), ("bread", 0.30), ("raw_pest", 0.10)),
    },
    "server_sub": {
        "name": "服务器房地板下", "level": 1, "weight": 8, "xp": 9, "danger": 0.22,
        "text": "冷却机组持续嗡鸣，活动地板下面藏着一处温热角落。",
        "drops": (("bio_scrap", 0.40), ("pheromone", 0.08)),
    },
    "wheat_crate": {
        "name": "Anteverse Wheat 货箱", "level": 1, "weight": 7, "xp": 11, "danger": 0.15,
        "text": "开裂的货箱里散落着几穗 Anteverse Wheat。",
        "drops": (("anteverse_wheat", 0.50), ("bio_scrap", 0.20)),
    },
    "crawlway": {
        "name": "主通风廊道", "level": 5, "weight": 7, "xp": 15, "danger": 0.28,
        "text": "胶带封住几处接缝，冷凝水在低处积成浅洼。",
        "drops": (("bio_scrap", 0.35), ("raw_pest", 0.25), ("pheromone", 0.15)),
    },
    "morgue_fridge": {
        "name": "冷藏储藏间", "level": 5, "weight": 6, "xp": 14, "danger": 0.25,
        "text": "一扇冷柜门没有关严，里面混着食材和待处理标本。",
        "drops": (("raw_pest", 0.35), ("rump", 0.32), ("bio_scrap", 0.15)),
    },
    "trap_corridor": {
        "name": "Pest 陷阱存放区", "level": 5, "weight": 5, "xp": 16, "danger": 0.30,
        "text": "一台尚未装入诱饵的 Pest 陷阱摆在墙边。",
        "drops": (("trap", 0.30), ("pheromone", 0.20), ("bio_scrap", 0.20)),
    },
    "containment": {
        "name": "隔音收容区", "level": 5, "weight": 5, "xp": 18, "danger": 0.35,
        "text": "八英尺高的屏障围住四周，吸音板压低了设备噪声。",
        "drops": (("pheromone", 0.30), ("bio_scrap", 0.30), ("rump", 0.15)),
    },
    "breach_av2": {
        "name": "Anteverse 2 接触区", "level": 12, "weight": 5, "xp": 26, "danger": 0.50,
        "text": "空气里带着静电；它在门区边缘停下，朝另一侧伏低身体。",
        "drops": (("pheromone", 0.25), ("rump", 0.25), ("trap", 0.15), ("bio_scrap", 0.20)),
    },
    "gantry_9": {
        "name": "高层货架", "level": 12, "weight": 4, "xp": 30, "danger": 0.45,
        "text": "上层货架与相邻箱堆之间留着一段足够它蓄跳的空隙。",
        "drops": (("rump", 0.30), ("trap", 0.20), ("pheromone", 0.20)),
    },
}

PLACE_NAME_MIGRATIONS = {
    "3B 通风管": "维护通风管",
    "休息室 14 号柜": "休息区储物柜",
    "食堂大厅": "食堂桌下",
    "服务器房地下层": "服务器房地板下",
    "小麦货箱": "Anteverse Wheat 货箱",
    "主通风廊道": "主通风廊道",
    "停尸间 B 柜": "冷藏储藏间",
    "捕兽夹走廊": "Pest 陷阱存放区",
    "收容大厅（8 英尺栏）": "隔音收容区",
    "Anteverse 2 破口": "Anteverse 2 接触区",
    "9 号天桥俯瞰台": "高层货架",
}

ANTICS = (
    "它用舌头解开了一个谜题盒。盒子里还有一个小一点的谜题盒。",
    "它操作了夹娃娃机，掉出一个迷你交通锥。",
    "它把带回来的东西藏到身下，五秒钟以后翻出来。",
    "它跟踪一团灰尘走了很远。",
    "它在走廊尽头站了很久。",
    "它从门缝里钻过去了。",
    "它跳得太高，消失了一会儿，回来的时候头上顶着一个纸杯。",
    "它把走廊里的箱子挨个推了一遍。",
    "它拖着比自己大两倍的东西走了半个走廊，然后放下不要了。",
    "它把通风口的螺丝拧松，又拧了回去。",
    "它在水洼里滚了一圈。",
    "它把一根电缆绕成一团。",
    "它对着自动贩卖机撞了三次，然后趴在机器底下。",
    "它从一堆零件里叼出一个，放到马桶里。",
    "它把舌头伸进管道，拽出来一只袜子。",
    "它在纸箱上咬出了两个洞。",
    "它用尖刺在纸箱上划了一道，退后两步，又划了一道。",
    "它把走廊里的灯开关按了四次。",
    "它抱着一颗螺丝趴了很久。",
    "它跟着自己转了两圈，然后停下来趴着。",
    "它把这里所有的东西都推到了同一边。",
    "它钻进一个空桶里，顶着桶横着走回来了。",
    "它钻进一个纸箱，只露出半截身子。",
    "它从通风口格栅的缝里挤了过去，卡住了一下。",
    "它把整个身子塞进一只鞋里，只有一根尖刺露在外面。",
    "它被自己的舌头绊了一下，翻了个身。",
    "它顺着桌子腿往上跳，跳到一半滑下来了。",
    "它躲进一堆碎纸里，只剩一条缝。",
    "它推不动那个箱子，绕到另一边再推了一次。",
    "它在原地转了三圈，找不到刚才要去的方向。",
    "它从桌子底下钻出来，头上顶着一片纸。",
    "它整个趴在地上，摊成一片。",
    "它用一根尖刺把自己撑起来，歇了一会儿。",
    "它从你脚边蹦过去，撞到门槛弹了回来。",
)

# 意外事件的后果权重：先按权重选一种，再从那一组的描写里随机取一条
HAZARD_WEIGHTS = (("leap", 0.40), ("tumble", 0.28),
                  ("startle", 0.22), ("moult", 0.10))

HAZARDS = {
    "leap": (
        "它在两个箱子之间跳了六个来回。",
        "它从地面直接跳上了货架顶层。",
        "它起跳时撞到了管道，落回原地，又跳了一次。",
        "它挂在天花板的管道上，挂了一会儿才下来。",
    ),
    "tumble": (
        "它跳歪了，滚进线缆槽。",
        "它落地时翻了半圈，撞在箱子上。",
        "它从货架上掉下来，弹了两下。",
    ),
    "startle": (
        "它钻进通风管道，躲了十分钟。",
        "它把尖刺全部立起来，原地停了三十秒。",
        "它弹起来撞到天花板，然后一路蹦没了。",
    ),
    "moult": (
        "它蜕皮了。地板上留了一小把尖刺。",
        "它蜕了一半的皮，剩下的挂在身上晃。",
        "它把蜕下来的皮吃掉了。",
    ),
}


EAT_LINES = (
    "它闻了闻，一口吞掉了。",
    "它用舌头卷走了。",
    "它把食物分成三份，再用舌头依次卷走。",
    "它整个趴到食物上去了。",
    "它把食物摁在箱子底部蹭了两下才吃。",
    "它先咬住，松口，再咬住，再松口，重复了六次。",
    "它把食物推到墙角，背对房间吃。",
    "它用舌头把食物抛起来，接住了。",
    "它把食物拖进自己的窝里。",
    "它叼着食物蹦上了箱子顶。",
    "它把食物顶在头上蹦了一圈，然后吃掉了。",
    "它咬了一口，把剩下的塞进箱子缝里。",
)


PET_LINES = (
    "它僵住，开始轻微震动。",
    "它整只压扁了一点，慢慢弹回原状。",
    "它把整个重量压在你手上。",
    "一根尖刺轻轻扎进你的指关节。",
    "舌头伸出来，在你手背上扫了一圈。",
    "它把舌尖伸出来，碰了碰你的指尖。",
    "它顺着你的袖子往上跳。",
    "它把舌头绕在你的手腕上，又松开。",
    "它翻过来，把尖刺收平，摊在你手心里。",
    "它用舌头勾住袖口，把你的手往投食口方向拉。",
    "它在你手上趴了两秒，然后跳下去。",
    "它往两只手之间缩了缩，只露出背上的尖刺。",
    "它用舌头把口袋盖掀开，探了进去。",
    "它用舌尖勾住鞋带，试着往后拖。",
)


STARVE_LINES = (
    "它把舌头伸进投食口，来回扫。",
    "它从你手边蹦开，重新守到投食口前。",
    "它伏低身体，把投食口附近的气味逐一舔过。",
    "它用舌头敲了敲空食盘。",
    "它把整个身子贴在投食口上。",
    "它用舌尖反复拨动投食口的搭扣。",
    "它把箱子里的碎屑全翻了一遍。",
    "它趴在投食口正下方，舌头伸在外面。",
)


FULL_LINES = (
    "翻过身躺着",
    "把这份食物推回你脚边",
    "整只压在上面",
    "把食物埋进箱角的碎屑里",
    "叼起来蹦了两下又放下",
    "用舌头把食物推回来",
    "把食物拨到箱子角落",
    "趴在上面不动",
)


AWAY_ANTICS = (
    "它把收容箱里的东西重新摆了一遍",
    "它把投食口拆了下来，装回去了",
    "它沿观察区内壁蹦了一整圈",
    "它把垫料全推到了同一个角落",
    "它伏到纸箱后面，只露出一圈尖刺",
    "它把一块碎屑叼在嘴里睡了很久",
)


OBSERVATIONS = (
    (85, (
        "它一蹦一蹦地跟着你穿过房间。",
        "它爬到你肩上待着。",
        "它贴着你的小腿落地，又立刻跟了上来。",
        "它从宠物床上跳下来，落在你脚边。",
        "你避开尖刺把它托起，它安静地待在掌心。",
    )),
    (60, (
        "你刚走近，它就从宠物床边蹦到了你脚边。",
        "它把投食口的盖子顶开一条缝。",
        "它占据了离你最近的那块地砖。",
        "它从桌底蹦出来，停在离你最近的箱子后。",
    )),
    (35, (
        "它朝你的方向转过身。",
        "它把舌头伸向你的方向，够不到。",
        "它一半身子埋在垫料里，一半在外面。",
        "它从垫料堆后面探出半个身子。",
    )),
    (15, (
        "它把箱子横在你们两个中间。",
        "它退到观察区最远的一侧。",
        "它整个缩进垫料堆，只留一根尖刺在外面。",
        "它伏在桌下，只从阴影里看着你。",
    )),
    (0, (
        "它伏低身体，舌头短促地弹了一下。",
        "你一靠近，它就跳到最远的箱子后。",
        "它把垫料全拱到身上，把自己埋了起来。",
    )),
)


STARVE_OBSERVATIONS = (
    "它守在投食口前，舌尖在空盘里来回扫。",
    "它在食物柜和你之间来回蹦。",
    "它整个贴在投食口上。",
    "它伏在投食口下，只盯着你手里的东西。",
)


# --- 小工具 ----------------------------------------------------------------
def clamp(value, low, high):
    return max(low, min(high, value))


def bar(value, width=10):
    filled = int(round(clamp(value, 0.0, 100.0) / 100.0 * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


def meter(label, value, text):
    return "%s\u3000%s  %s" % (label, bar(value), text)


def fmt_dur(seconds):
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return "%d 天 %d 小时" % (days, hours)
    if hours:
        return "%d 小时 %02d 分" % (hours, mins)
    if mins:
        return "%d 分 %02d 秒" % (mins, secs)
    return "%d 秒" % secs


def xp_needed(level):
    return int(28 * (level ** 1.45))


def vigor_max(state):
    return VIGOR_BASE + VIGOR_PER_LEVEL * (state["level"] - 1)


def observe(state):
    """回到收容箱时看到的那一个动作。"""
    if state["hunger"] <= 0.0:
        return random.choice(STARVE_OBSERVATIONS)
    for threshold, actions in OBSERVATIONS:
        if state["bond"] >= threshold:
            return random.choice(actions)
    return random.choice(OBSERVATIONS[-1][1])


def clean_name(raw):
    """把用户输入里的 @ 提及和 Discord markdown 处理掉。"""
    text = (raw or "").strip()
    text = "".join(ch for ch in text if ch.isprintable())
    for junk in ("`", "*", "_", "~", "|", "\\"):
        text = text.replace(junk, "")
    text = re.sub(r"\s+", " ", text).strip()
    # 必须在过滤之后再处理：零宽空格不是可打印字符，先插会被当成杂字符删掉，
    # 结果就是一个能真的 @ 到人的提及。
    text = text.replace("<", "\u2039").replace(">", "\u203a").replace("@", "\u200b@")
    return text[:24]


def cmd(name):
    """给用户看的子命令写法，永远带空格。"""
    return ("%s %s" % (PREFIX, name)).strip()


def give_xp(state, amount):
    """加经验，把跨过的每一级都结算掉。返回升了几级。"""
    amount = int(round(amount))
    if amount <= 0:
        return 0
    state["xp"] += amount
    gained = 0
    while state["xp"] >= xp_needed(state["level"]):
        state["xp"] -= xp_needed(state["level"])
        state["level"] += 1
        gained += 1
        state["tongue"] += 1
        if state["level"] % 2 == 0:
            state["spikes"] += 1
        if state["level"] % 3 == 0:
            state["jump"] += 5
    return gained


def add_bond(state, amount):
    before = state["bond"]
    state["bond"] = clamp(state["bond"] + amount, 0.0, BOND_CAP)
    return state["bond"] - before


def weighted_choice(options):
    """options 是 (key, weight[, payload])，返回整个 option。"""
    total = 0.0
    for option in options:
        total += float(option[1])
    if total <= 0:
        return options[0]
    roll = random.random() * total
    for option in options:
        roll -= float(option[1])
        if roll <= 0:
            return option
    return options[-1]


def cd_left(state, name, span):
    """冷却还剩几秒；读坏了就当没有冷却。"""
    try:
        last = float(state["cd"].get(name) or 0)
    except (TypeError, ValueError):
        last = 0.0
    return span - (time.time() - last)


# --- 状态 ------------------------------------------------------------------
def new_state(now):
    return {
        "v": STATE_VERSION,
        "name": "Pest",
        "keeper": None,
        "keeper_name": None,
        "born": now,
        "level": 1,
        "xp": 0,
        "bond": 0.0,
        "hunger": 55.0,
        "vigor": 70.0,
        "entropy": 0.0,
        "jump": 100,
        "spikes": 3,
        "tongue": 12,
        "size_len": 25,
        "size_wid": 15,
        "where": "收容箱",
        "last": now,
        "renamed_at": 0.0,
        "cd": {},
        "bag": {"bio_scrap": 2, "nachos": 1},
        "codex": {},
        "tally": {"feeds": 0, "explores": 0, "pets": 0, "moults": 0,
                  "starved": 0, "found": 0, "leaps": 0, "scares": 0, "snacks": 0},
    }


NUM_FIELDS = ("level", "xp", "bond", "hunger", "vigor", "entropy", "jump",
              "spikes", "tongue", "size_len", "size_wid",
              "last", "born", "renamed_at")


def sane_state(state):
    """存下来的状态能不能信。不能就丢掉重建。"""
    if not isinstance(state, dict) or state.get("v") != STATE_VERSION:
        return None
    for field in NUM_FIELDS:
        value = state.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    for field in ("name", "where"):
        if not isinstance(state.get(field), str):
            return None
    for field in ("keeper", "keeper_name"):
        if state.get(field) is not None and not isinstance(state.get(field), str):
            return None
    for field in ("cd", "bag", "codex", "tally"):
        if not isinstance(state.get(field), dict):
            return None
    for key, count in state["bag"].items():
        if key not in ITEMS or isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return None
    for key in state["codex"]:
        if key not in PLACES:
            return None
    if state["level"] < 1:
        return None
    return state


def read_state():
    """读出全服那一只 Pest。

    KV 存的是整个库，平台在两个 store 里的嵌套深度并不一致
    （测试库是 {"pest": {...}}，正式库可能直接就是 {...}），
    所以这里两种形状都认，省得换个 store 就静默重置。
    """
    node = kv_get(STATE_KEY)
    for _ in range(4):                       # 最多剥四层，够覆盖各种套法
        if sane_state(node) is not None:
            return node
        if not isinstance(node, dict) or not node:
            break
        nxt = node.get(STATE_KEY)
        if nxt is None:                      # 没有约定的键，就试着往里找一层
            nxt = next(iter(node.values()))
        node = nxt
    store = kv_all()
    if sane_state(store) is not None:
        return store
    return None


def load_state(state):
    """整个服务器只有一只 Pest，所有频道共用同一份状态。"""
    now = time.time()
    state = sane_state(state)
    if state is None:
        state = new_state(now)
    tally = new_state(now)["tally"]
    tally.update({k: v for k, v in state["tally"].items()
                  if isinstance(v, (int, float)) and not isinstance(v, bool)})
    state["tally"] = tally
    state["where"] = PLACE_NAME_MIGRATIONS.get(state["where"], state["where"])
    state["cd"].pop("pet", None)
    report = tick(state, now)
    return state, report


def add_to_bag(state, key, count=1):
    count = int(count)
    if count <= 0:
        return
    bag = state["bag"]
    if key not in bag and len(bag) >= BAG_KIND_CAP:
        # 别无限膨胀：丢掉最不值钱的那种
        worst = min(bag, key=lambda k: (ITEMS[k]["tier"], bag[k]))
        if ITEMS[worst]["tier"] > ITEMS[key]["tier"]:
            return
        del bag[worst]
    bag[key] = int(bag.get(key, 0)) + count
    state["tally"]["found"] += count


def best_food(state):
    """没人看着的时候，它会先吃背包里最好的那一份。"""
    best = None
    for key, count in state["bag"].items():
        item = ITEMS.get(key)
        if not item or item["kind"] == "tool" or count <= 0:
            continue
        rank = (item["nourish"], -item["bond"], -item["tier"])
        if best is None or rank > (ITEMS[best]["nourish"], -ITEMS[best]["bond"],
                                   -ITEMS[best]["tier"]):
            best = key
    return best


# --- 离线结算 --------------------------------------------------------------
def tick(state, now=None):
    """按真实时间把宠物往前推。返回一个「你不在的时候」汇报，或者 None。"""
    now = float(now if now is not None else time.time())
    last = float(state.get("last") or now)
    hours = (now - last) / 3600.0
    if hours <= 0.0:
        state["last"] = now
        return None
    hours = min(hours, OFFLINE_CAP_H)

    starving = state["hunger"] <= 0.0
    state["hunger"] = clamp(state["hunger"] - HUNGER_PER_H * hours, 0.0, 100.0)
    state["vigor"] = clamp(state["vigor"] + VIGOR_PER_H * hours, 0.0, vigor_max(state))
    state["entropy"] = clamp(state["entropy"] - ENTROPY_PER_H * hours, 0.0, 100.0)
    state["last"] = now

    report = {"hours": hours, "notes": [], "starved": False, "levels": 0, "xp": 0}

    if state["hunger"] <= 0.0:
        snack = best_food(state)
        if snack:
            # 与其饿死，不如去翻自己的包
            state["bag"][snack] -= 1
            if state["bag"][snack] <= 0:
                del state["bag"][snack]
            item = ITEMS[snack]
            state["hunger"] = clamp(state["hunger"] + item["nourish"] * 0.8, 0.0, 100.0)
            state["entropy"] = clamp(state["entropy"] + item["entropy"] * 0.3, 0.0, 100.0)
            state["tally"]["snacks"] += 1
            report["notes"].append("自己把 %s 吃了" % item["name"])
        else:
            add_bond(state, -STARVE_BOND_PER_H * hours)
            if starving:
                state["tally"]["starved"] += 1
            report["starved"] = True
        # 饥饿时记录额外的觅食行为
        if random.random() < 0.5:
            report["notes"].append(random.choice(AWAY_ANTICS))

    # 顺手捡东西：它一直都在
    scavenge = SCAVENGE_PER_H * hours
    rolls = int(scavenge) + (1 if random.random() < (scavenge - int(scavenge)) else 0)
    for _ in range(rolls):
        add_to_bag(state, random.choice(SCAVENGE_POOL))

    report["xp"] = int(IDLE_XP_PER_H * hours)
    report["levels"] = give_xp(state, report["xp"])
    if hours < AWAY_MIN_H:
        return None
    return report


def with_away(text, state, report):
    """把「你不在的时候」那段汇报拼到正文前面；没有内容就什么都不加。"""
    line = away_line(state, report)
    return line + "\n\n" + text if line else text


def away_line(state, report):
    """回来时的那段叙述。"""
    if not report:
        return ""
    who = state["name"]
    bits = []
    if report["levels"]:
        bits.append("长大了 %d 级" % report["levels"])
    if report["xp"]:
        bits.append("+%d 经验" % report["xp"])
    bits.extend(report["notes"])
    if report["starved"]:
        bits.append("它饿着")
    if not bits:
        return ""
    return "你不在的时候，%s：%s。" % (who, "、".join(bits))


# --- 文本 ------------------------------------------------------------------
def status_text(state, report):
    need = xp_needed(state["level"])
    top = vigor_max(state)
    lines = ["\U0001F954 **%s** \u00b7 Lv%d" % (state["name"], state["level"]),
             meter("经验", state["xp"] * 100.0 / max(1, need), "%d/%d" % (state["xp"], need)),
             meter("信任", state["bond"], "%d%%" % int(state["bond"])),
             meter("饱食", state["hunger"], "%d%%" % int(state["hunger"])),
             meter("体力", state["vigor"] * 100.0 / max(1, top), "%d/%d" % (int(state["vigor"]), top)),
             "",
             observe(state)]
    lines.append("IS-0173-A \u00b7 身处%s" % state["where"])
    lines.append("已探索 %d/%d 个地点 \u00b7 累计 %d 次探索、%d 次投喂、%d 次抚摸"
                 % (len(state["codex"]), len(PLACES), state["tally"]["explores"],
                    state["tally"]["feeds"], state["tally"]["pets"]))
    return with_away("\n".join(lines), state, report)


def bag_text(state, report):
    bag = state["bag"]
    if not bag:
        return with_away("包是空的。先用 `%s` 让它出去转一圈。" % cmd("explore"),
                         state, report)

    ranked = sorted(bag.items(), key=lambda kv: (ITEMS[kv[0]]["kind"] == "tool",
                                                   ITEMS[kv[0]]["tier"], -kv[1]))
    lines = ["**%s 的背包** · %d 样东西，共 %d 件" %
             (state["name"], len(bag), sum(bag.values()))]
    feed_items = [(key, count) for key, count in ranked if ITEMS[key]["kind"] != "tool"]
    found_items = [(key, count) for key, count in ranked if ITEMS[key]["kind"] == "tool"]

    if feed_items:
        lines.append("\n**可投喂物**")
        for key, count in feed_items:
            item = ITEMS[key]
            lines.append("%s **%s** x%d　_饱食%+d · 信任%+d · 经验%+d_" %
                         (item["emoji"], item["name"], count, item["nourish"],
                          item["bond"], item["xp"]))
    if found_items:
        lines.append("\n**探索收获**")
        for key, count in found_items:
            item = ITEMS[key]
            lines.append("%s **%s** x%d　_不可投喂_" %
                         (item["emoji"], item["name"], count))
    return with_away("\n".join(lines), state, report)


def codex_text(state, report):
    lines = []
    for key, place in PLACES.items():
        if key in state["codex"]:
            mark = "已去过 %d 次" % state["codex"][key]
        elif state["level"] >= place["level"]:
            mark = "还没去过"
        else:
            mark = "Lv%d 之后才去得了" % place["level"]
        lines.append("%s\u3000%s（%s）" % ("\U0001F5F3", place["name"], mark))
    text = ("**设施地点图鉴** \u00b7 已探索 %d/%d。头一次去会有额外经验。"
            % (len(state["codex"]), len(PLACES)))
    if state["codex"]:
        favourite = max(state["codex"], key=lambda k: state["codex"][k])
        text += "\n去得最多的是%s，%d 次。" % (PLACES[favourite]["name"], state["codex"][favourite])
    return with_away(text + "\n" + "\n".join(lines), state, report)


def item_field(key, item, held):
    if item["kind"] == "tool":
        effect = "探索收获 · 不可投喂"
    else:
        effect = "本项目效果 · 饱食%+d · 信任%+d · 经验%+d" % (
            item["nourish"], item["bond"], item["xp"])
    value = "带着 %d\n%s\n%s" % (held, effect, item["description"])
    if item["flavor"]:
        value += "\n_%s_" % item["flavor"]
    return {"name": "%s %s" % (item["emoji"], item["name"]),
            "value": value, "inline": False}


def items_embed(state, report):
    ranked = sorted(ITEMS.items(), key=lambda kv: (kv[1]["kind"] == "tool", kv[1]["tier"]))
    fields = []
    feed_items = [(key, item) for key, item in ranked if item["kind"] != "tool"]
    found_items = [(key, item) for key, item in ranked if item["kind"] == "tool"]
    if feed_items:
        fields.append({"name": "可投喂物", "value": "Wiki 说明与本项目投喂效果", "inline": False})
        fields.extend(item_field(key, item, int(state["bag"].get(key, 0)))
                     for key, item in feed_items)
    if found_items:
        fields.append({"name": "探索收获", "value": "工具不能投喂", "inline": False})
        fields.extend(item_field(key, item, int(state["bag"].get(key, 0)))
                     for key, item in found_items)
    description = away_line(state, report)
    return {"title": "物品说明", "description": description or None,
            "color": 0x5865F2, "fields": fields}


HELP_ROWS = (
    (cmd("pet"), "抚摸"),
    (cmd("explore"), "让它出去转一圈"),
    (cmd("feed [物品] [数量]"), "喂它；不给就自动挑一份"),
    (cmd("rename <名字>"), "给它起名"),
    (cmd("bag"), "背包"),
    (cmd("codex"), "去过的地点"),
    (cmd("items"), "物品说明"),
    (cmd("help"), "这页"),
)
HELP_ALIASES = ((cmd("go"), "探索"), (cmd("inv"), "背包"),
                (cmd("dex"), "图鉴"), (cmd("name"), "改名"))


def help_text(state, report):
    lines = ["**非生物因素**：Feed the Pest。收容箱里关着 IS-0173-A。", ""]
    lines += ["`" + c + "`\u3000" + d for c, d in HELP_ROWS]
    lines += ["",
              "别名：" + "，".join("`" + a + "` = " + w for a, w in HELP_ALIASES),
              "饱食每小时 -%d，体力每小时 +%d。" % (int(HUNGER_PER_H), int(VIGOR_PER_H))]
    return "ok", "\n".join(lines)


def vitals_lines(state):
    top = vigor_max(state)
    return [meter("饱食", state["hunger"], "%d%%" % int(state["hunger"])),
            meter("体力", state["vigor"] * 100.0 / max(1, top),
                  "%d/%d" % (int(state["vigor"]), top))]


# --- 命令 ------------------------------------------------------------------
def cmd_status(state, who, args, report):
    return "ok", status_text(state, report)


def cmd_bag(state, who, args, report):
    return "ok", bag_text(state, report)


def cmd_codex(state, who, args, report):
    return "ok", codex_text(state, report)


def cmd_items(state, who, args, report):
    return "embed", items_embed(state, report)


def cmd_help(state, who, args, report):
    # help_text 自己就返回 (kind, text)
    return help_text(state, report)


def cmd_rename(state, who, args, report):
    if not args:
        return "err", "起个名字：`%s 大号土豆`" % cmd("rename")
    if len(args) > 60:
        return "err", "名字太长了，24 个字封顶。"
    name = clean_name(args)
    if len(name) < 2:
        return "err", "这个名字太短了。至少给两个字符。"
    if name.lower() == state["name"].lower():
        return "err", "它现在就叫%s。" % state["name"]
    cooldown = cd_left(state, "rename", RENAME_CD)
    if cooldown > 0:
        return "err", "名字换一次要等 %s。" % fmt_dur(cooldown)

    first_claim = state.get("keeper") is None
    state["name"] = name
    state["cd"]["rename"] = time.time()
    state["keeper"] = who[0]
    state["keeper_name"] = who[1]
    if first_claim:
        text = "\U0001F954 它跳了一下，就当它听懂了吧。\n从现在起它叫 **%s**。" % name
    else:
        text = "\U0001F954 它又跳了一下。\n从现在起它叫 **%s**。" % name
    return "ok", text


def cmd_explore(state, who, args, report):
    cooldown = cd_left(state, "explore", EXPLORE_CD)
    if cooldown > 0:
        return "err", "它还在探索，预计 %s 后回来。" % fmt_dur(cooldown)
    if state["hunger"] <= 0.0:
        return "err", "它守在投食口前，不肯出发。先喂点东西：`%s <物品>`" % cmd("feed")
    if state["vigor"] < 10.0:
        return "err", ("它伏在宠物床旁，没有起跳。体力 %d/%d，每小时回 %d。"
                       % (int(state["vigor"]), vigor_max(state), VIGOR_PER_H))

    open_keys = [k for k in PLACES if PLACES[k]["level"] <= state["level"]]
    key, _weight = weighted_choice([(k, PLACES[k]["weight"]) for k in open_keys])
    place = PLACES[key]

    state["cd"]["explore"] = time.time()
    state["tally"]["explores"] += 1

    lines = ["**%s**" % place["name"], place["text"]]
    fresh = key not in state["codex"]
    if fresh:
        state["codex"][key] = 1
        lines.append("新地方。")
    else:
        state["codex"][key] += 1

    loot = []
    for item_key, chance in place["drops"]:
        if random.random() < chance:
            count = 2 if random.random() < 0.15 else 1
            add_to_bag(state, item_key, count)
            item = ITEMS[item_key]
            loot.append("%s x%d" % (item["name"], count))
    has_loot = bool(loot)

    if random.random() < 0.75:
        lines.append(random.choice(ANTICS))

    xp = float(place["xp"])
    if fresh:
        xp *= 1.75
    if state["hunger"] > 70:
        xp *= 1.25
    state["hunger"] = clamp(state["hunger"] + 3.0, 0.0, 100.0)
    state["vigor"] = clamp(state["vigor"] - (9.0 + place["level"] / 4.0), 0.0, vigor_max(state))
    state["where"] = place["name"]

    if random.random() < place["danger"]:
        kind, _weight = weighted_choice(list(HAZARD_WEIGHTS))
        lines.append(random.choice(HAZARDS[kind]))
        if kind == "leap":
            gain = random.randint(2, 6)
            state["jump"] += gain
            state["tally"]["leaps"] += 1
            xp += 4
            lines.append("起跳高度 +%d%%。" % gain)
        elif kind == "tumble":
            state["vigor"] = clamp(state["vigor"] - 8.0, 0.0, vigor_max(state))
            lines.append("它没受伤。")
        elif kind == "startle":
            add_bond(state, -2.0)
            state["tally"]["scares"] += 1
            lines.append("信任掉了一点。")
        elif kind == "moult":
            state["spikes"] += 2
            state["tally"]["moults"] += 1
            xp += 6
            lines.append("多了两根尖刺。")

    levels = give_xp(state, xp)
    lines.append("获得 %s，获得 %d 经验。" % ("、".join(loot), int(xp)) if has_loot
                 else "什么也没带回来，获得 %d 经验。" % int(xp))
    lines.extend(vitals_lines(state))
    if levels:
        lines.insert(len(lines) - 2,
                     "\U0001F389 %s 升到了 Lv%d，体力上限变成 %d。"
                     % (state["name"], state["level"], vigor_max(state)))
    if report:
        line = away_line(state, report)
        if line:
            lines.insert(0, line + "\n")
    return "ok", "\n".join(lines)


def resolve_item(state, token):
    """把随意的输入（nacho、rump、 pheromone）对上背包里真有的东西。"""
    token = token.strip().lower()
    if not token:
        return None, None
    held = [key for key, count in state["bag"].items() if count > 0]
    for key in held:
        item = ITEMS[key]
        if key == token or item["name"].lower() == token:
            return key, item
    for key in held:
        if key.startswith(token) or token in key:
            return key, ITEMS[key]
    for key in held:
        if token in ITEMS[key]["name"].lower():
            return key, ITEMS[key]
    return None, None


def cmd_feed(state, who, args, report):
    if not args:
        # 不给参数就直接喂：挑背包里最好的那份
        held = [k for k, n in state["bag"].items()
                if n > 0 and ITEMS[k]["kind"] != "tool"]
        if not held:
            if state["bag"]:
                return "err", "包里只有探索收获，不能投喂。先用 `%s`。" % cmd("explore")
            return "err", "包是空的。先用 `%s`。" % cmd("explore")
        args = max(held, key=lambda k: (ITEMS[k]["nourish"], ITEMS[k]["bond"]))

    cooldown = cd_left(state, "feed", FEED_CD)
    if cooldown > 0:
        return "err", "它还在嚼，%s。" % fmt_dur(cooldown)

    parts = args.split()
    key, item = resolve_item(state, parts[0])
    if key is None:
        return "err", "包里没有%s。`%s` 列了它能吃的。" % (parts[0], cmd("items"))
    if item["kind"] == "tool":
        return "err", "%s 属于探索收获，不能投喂。" % item["name"]

    count = 1
    if len(parts) > 1:
        try:
            count = int(parts[1])
        except ValueError:
            return "err", "喂几份？`%s %s 3`" % (cmd("feed"), parts[0])
    count = max(1, min(MAX_FEED_N, count, int(state["bag"][key])))
    if (state["hunger"] >= 98.0 and item["nourish"] > 0 and item["bond"] <= 0):
        return "err", "它撑着了，%s。" % random.choice(FULL_LINES)

    state["cd"]["feed"] = time.time()
    state["bag"][key] -= count
    if state["bag"][key] <= 0:
        del state["bag"][key]
    state["tally"]["feeds"] += 1

    nourishment = min(100.0 - state["hunger"], item["nourish"] * count)
    state["hunger"] = clamp(state["hunger"] + nourishment, 0.0, 100.0)
    state["entropy"] = clamp(state["entropy"] + item["entropy"] * min(count, 3) * 0.7, 0.0, 100.0)
    gained = add_bond(state, item["bond"] * count)
    xp_amount = item["xp"] * count
    levels = give_xp(state, xp_amount)

    lines = ["**%s x%d**" % (item["name"], count), item["description"]]
    if item["flavor"]:
        lines.append("_%s_" % item["flavor"])
    lines.append(random.choice(EAT_LINES))
    if gained > 0:
        lines.append("信任 +%d。" % int(round(gained)))
    elif gained < 0:
        lines.append("信任 %d。" % int(round(gained)))
    lines.append("获得 %d 经验。" % int(xp_amount))
    if levels:
        lines.append("\U0001F389 %s 升到了 Lv%d，体力上限变成 %d。"
                     % (state["name"], state["level"], vigor_max(state)))
    lines.extend(vitals_lines(state))
    if report:
        line = away_line(state, report)
        if line:
            lines.insert(0, line + "\n")
    return "ok", "\n".join(lines)


def cmd_pet(state, who, args, report):
    state["tally"]["pets"] += 1

    name = state["name"]
    if state["hunger"] <= 0.0:
        text = "%s 摸了摸 %s。%s" % (who[1], name, random.choice(STARVE_LINES))
        return "ok", with_away(text, state, report)

    add_bond(state, random.uniform(1.5, 3.5) + (0.5 if args else 0.0))
    text = "%s 摸了摸 %s。%s" % (who[1], name, random.choice(PET_LINES))
    return "ok", with_away(text, state, report)

ROUTES = {
    "": cmd_status,
    "status": cmd_status,
    "pet": cmd_pet,
    "bag": cmd_bag,
    "inv": cmd_bag,
    "inventory": cmd_bag,
    "codex": cmd_codex,
    "dex": cmd_codex,
    "items": cmd_items,
    "shelf": cmd_bag,
    "help": cmd_help,
    "rename": cmd_rename,
    "name": cmd_rename,
    "explore": cmd_explore,
    "go": cmd_explore,
    "feed": cmd_feed,
}


# --- 平台入口 --------------------------------------------------------------
def on_message(message):
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, str):
        return
    text = content.strip()
    if not text.lower().startswith(PREFIX):
        return
    rest = text[len(PREFIX):]
    if rest and not rest[0].isspace():
        return  # 「!pestcontrol」不是我们的命令

    parts = rest.split(None, 1)
    command = parts[0].lower() if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = ROUTES.get(command)
    author = message.get("author") or {}
    who = (str(author.get("id") or "anon"),
           str(author.get("display_name") or author.get("name") or "某人"))

    state, report = load_state(read_state())

    if handler is None:
        # 回显用户输入之前先过一遍：原样贴回去可能带进控制字符
        shown = clean_name(command)
        kind = "err"
        body = ("没有「%s」这个指令。`%s` 列了全部。" % (shown, cmd("help")) if shown
                else "没有这条指令。`%s` 列了全部。" % cmd("help"))
    else:
        kind, body = handler(state, who, args, report)

    kv_set(STATE_KEY, state)
    if kind == "embed":
        send_embed(title=body["title"], description=body["description"],
                   color=body["color"], fields=body["fields"])
    else:
        send(body)


def on_failure(event_name, event_data, error):
    log("[%s] failed: %r" % (event_name, error))
