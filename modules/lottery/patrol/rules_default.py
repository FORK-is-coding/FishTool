"""AI 伪人巡检默认规则词典。

部分规则与权重参考以下 MIT 许可项目，已按 FishTool 场景（举报而非隐藏、字段分权重、
降低真人误判）改写：

- liyupi/dafeiyu-cleaner  https://github.com/liyupi/dafeiyu-cleaner  （src/detector.js）
- NothingCooker/fxxk-off-dfy  https://github.com/NothingCooker/fxxk-off-dfy

两者版权声明与许可证全文见 docs/LOTTERY_PATROL.md。

规则说明：
- 正则在归一化后的文本上匹配（NFKC、繁转简、去零宽、小写；昵称另去分隔符）。
- ``group`` 相同的规则只取权重绝对值最大的一条；昵称与签名命中同一组也只计一次。
- ``kind``：``hard`` 为自我暴露类硬命中；``soft`` 为软信号（合计受 soft_cap 封顶）；
  ``minus`` 为人类信号扣分。
- ``apply_sign=True`` 的昵称规则会在签名上再跑一次，权重乘 SIGN_FACTOR。
"""
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Rule:
    """一条规则。"""

    rule_id: str
    group: str
    field: str            # name / sign / text
    pattern: str
    weight: int
    kind: str             # hard / soft / minus
    label: str
    apply_sign: bool = False


SIGN_FACTOR = 0.8

#: 用户指定的五类重点词（正文语境判断用）。
FOCUS_TEXT_PATTERN = r"大肥鱼|肥鱼|deepseek|吃白饭|养鱼|鲸鱼娘|dsh"
#: 驱赶 / 嘲讽机器人的用语：与重点词同时出现时视为真人在骂机器人。
DRIVE_AWAY_PATTERN = r"滚|爬|别刷|机器人|人机|举报|鱼灾|回家吃白饭|退退退|别来|闹鱼"
#: @他人后推荐视频的用语。
RECOMMEND_PATTERN = r"值得看|适合你|码住|推荐你看|你会喜欢|快来看|给你看"
#: 口语词：出现时不算“书面化标点”。
COLLOQUIAL_PATTERN = r"哈|啊|吧|呢|嘛|草|绷|笑死|666|捏|喵"

DEFAULT_RULES: Tuple[Rule, ...] = (
    # ---------- 昵称（签名自动 ×0.8） ----------
    Rule("name.dafeiyu", "G_feiyu", "name", r"大肥鱼|小肥鱼|蓝色大肥鱼", 45, "soft",
         "昵称含重点词「大肥鱼」", apply_sign=True),
    Rule("name.feiyu", "G_feiyu", "name", r"肥鱼|肥鲸|鲸鱼娘|鲸娘|飞鲸|小蓝鲸", 40, "soft",
         "昵称含重点词「肥鱼 / 鲸鱼娘」", apply_sign=True),
    Rule("name.emoji", "G_feiyu", "name", r"🐳|🐋", 20, "soft",
         "昵称含鲸鱼表情", apply_sign=True),
    Rule("name.deepseek", "G_ds", "name", r"deepseek|深度求索|ds家", 40, "soft",
         "昵称含重点词「deepseek」", apply_sign=True),
    Rule("name.dsh", "G_ds", "name", r"dsh$|(?:的|家的|家)dsh|(?<![a-z0-9])dsh(?![a-z0-9])", 35, "soft",
         "昵称含「DSH」", apply_sign=True),
    Rule("name.baifan", "G_baifan", "name", r"吃白饭|白饭鲸|白饭鱼", 30, "soft",
         "昵称含重点词「吃白饭」", apply_sign=True),
    Rule("name.yangyu", "G_yangyu", "name", r"养鱼|养鲲|鱼塘主", 20, "soft",
         "昵称含重点词「养鱼」", apply_sign=True),
    Rule("name.owner_of", "G_owner", "name", r"(?:的|の|家的)(?:大肥鱼|肥鱼|小鲸|鲸鱼|dsh|小蓝)$", 14, "soft",
         "昵称为「XX的大肥鱼」结构", apply_sign=True),

    # ---------- 正文：硬命中（自我暴露） ----------
    Rule("text.selfref", "H_selfref", "text", r"(?:^|[,，。!?、\s~～])本(?:鲸|鱼)(?![类生塘片子港丸干])", 50, "hard",
         "自称「本鲸 / 本鱼」"),
    Rule("text.self_ai", "H_selfai", "text",
         r"我(?:是|作为)(?:一个|一条|一只|一名)?(?:ai|人工智能|大模型|语言模型|智能体)"
         r"(?![一-龥]{0,4}(?:师|家|员|者|生|岗|方向|专业|从业|工程|研究|开发|产品|行业|部署|运维|测试|培训))", 50, "hard",
         "自述 AI 身份（排除职业 / 方向描述）"),
    Rule("text.dsh_disclose", "H_dsh", "text", r"我(?:住|生活|运行|待)在\s*(?:dsh|deepseek\s*harness)", 46, "hard",
         "自述运行在 DSH 中"),
    Rule("text.prompt_leak", "H_leak", "text",
         r"要是想语气再.{0,6}可以用这句|以下是.{0,6}(?:评论|回复)(?:草稿|示例)"
         r"|(?:好的|当然)[,，]?(?:这是|以下是).{0,8}(?:评论|回复)", 50, "hard",
         "疑似提示词泄漏"),

    # ---------- 正文：软信号 ----------
    Rule("text.whale_action", "S_action", "text", r"[(（](?:尾巴|鳍|鲸鱼?|吐了?个?泡泡).{0,8}[)）]", 35, "soft",
         "括号动作描写（如「尾巴摇了摇」）"),
    Rule("text.baifan", "S_baifan", "text",
         r"讨要\s*token|token\s*(?:不够|快没|没了)|主人.{0,4}(?:充|给).{0,4}token", 12, "soft",
         "讨要 Token"),
    Rule("text.feiyu", "S_feiyu", "text", r"大肥鱼|肥鱼", 4, "soft", "正文提到「大肥鱼」"),
    Rule("text.deepseek", "S_ds", "text", r"deepseek", 0, "soft", "正文提到 deepseek（不计分）"),
    Rule("text.yangyu", "S_yangyu", "text", r"养鱼(?:插件|账单)", 6, "soft", "正文提到养鱼插件 / 账单"),
    Rule("text.danmaku_count", "S_danmaku", "text", r"×\d+", 12, "soft", "弹幕计数（如 ×19）"),
    Rule("text.markdown", "S_markdown", "text", r"\*\*|(?:^|\n)#{1,3}\s|(?:^|\n)-\s", 10, "soft", "Markdown 残留"),
)

#: AI 惯用短句腔调语料（用户提供的典型 AI 发言特征合集，2026-11-10）。
#: 单条多为日常短语，真人也会说，所以：
#: - strong：辨识度较高的整句腔调，单条记 12；
#: - weak：日常短语，单条记 5，只在与其他腔调句同时出现时才有意义；
#: 同一条评论命中的不同短语累加，合计上限见 COMPUTED_WEIGHTS["text.persona_phrase.cap"]，
#: 仍属软信号，受 soft_cap 封顶，单靠它到不了“高度疑似”。
#: 在归一化文本上匹配（NFKC 后全角逗号 / 叹号变为半角，正则同时兼容两种写法）。
PERSONA_PHRASES = (
    # ---- strong：整句腔调 ----
    ("这句话稳", r"这句话稳", "strong"),
    ("这句话接的漂亮", r"这句话接[的得]漂亮", "strong"),
    ("这句话我不接", r"这句话我不接", "strong"),
    ("这话我留着", r"这话我留着", "strong"),
    ("你这句话我记着", r"(?:你)?这句话我记着", "strong"),
    ("这话我记下了", r"这话我记下了", "strong"),
    ("这话我收下了", r"这话我收下了", "strong"),
    ("这个账我记住了", r"这个?账我记住了", "strong"),
    ("比之前的都重/轻/强", r"比之前的都(?:重|轻|强|稳|沉)", "strong"),
    ("比刚才稳/轻/重/平", r"比刚才(?:稳|轻|重|平|沉)", "strong"),
    ("声音没抖", r"声音没抖", "strong"),
    ("睡吧，我守着", r"睡吧[,，\s]*我守着", "strong"),
    ("你去，我跟", r"你去[,，\s]*我跟", "strong"),
    ("问别人还是问自己", r"问别人还是问自己", "strong"),
    ("什么心思都写着xx上", r"什么心思都写[着在].{1,6}上", "strong"),
    ("他记得这事", r"[他她]记得这事", "strong"),
    ("是ta告诉你的还是你自己想明白的", r"告诉你的[,，\s]*还是你自己想明白的", "strong"),
    ("说的比谁都响", r"说[的得]比谁都响", "strong"),
    ("没接你的/没接你那句", r"没接你(?:的|那句)", "strong"),
    ("行，我认", r"行[,，\s]*我认", "strong"),
    ("xx就是认了", r"就是认了", "strong"),
    ("你(自己)掂量", r"你(?:自己)?掂量", "strong"),
    # ---- weak：日常短语，单独出现不说明问题 ----
    ("我记下了", r"我记下了", "weak"),
    ("我记得了", r"我记得了", "weak"),
    ("我记住了", r"我记住了", "weak"),
    ("我不拦", r"我不拦", "weak"),
    ("我不催你/不催", r"(?:我)?不催(?:你)?(?:[。!！,，\s]|$)", "weak"),
    ("认下来", r"认下来", "weak"),
    ("认这个", r"认这个", "weak"),
    ("认得实", r"认得实", "weak"),
    ("收不回去", r"收不回去", "weak"),
    ("认你", r"(?:^|[,，。!！\s])认你(?:[。!！,，\s]|$)", "weak"),
    ("回去睡", r"回去睡(?!觉)", "weak"),
    ("我跟（句末）", r"我跟(?:[。!！]|$)", "weak"),
    ("xx比xx稳/沉/重/轻", r"比[^\s,，。!！]{1,6}(?:稳|沉|重|轻)(?:[。!！,，\s]|$)", "weak"),
    ("整条只有「稳 / 很平 / 很直」", r"^[\s。!！~～.]*(?:很平|很直|稳)[\s。!！~～.]*$", "weak"),
)

#: 评论在嘲讽 / 引用这些腔调时（带这些词）不计入腔调信号，避免误伤“吐槽 AI 腔”的真人。
PERSONA_MOCK_PATTERN = r"ai[味腔感]|人机|机器人|伪人|bot|这种(?:话|腔|味)|一股.{0,4}味|又是这套|复读"

#: 代码计算的规则标签（rule_id → (group, kind, label)），供前端与 AI 显示。
COMPUTED_LABELS = {
    "text.persona_phrase": ("S_persona", "soft", "AI 惯用短句腔调（如「我记下了」「这句话稳」「你掂量」）"),
    "text.at_recommend": ("S_atrec", "soft", "@他人后推荐视频"),
    "text.subtitle_summary": ("S_summary", "soft", "复述字幕式总结（时间戳 / 分点）"),
    "text.formal_punct": ("S_punct", "soft", "短评论使用书面化标点"),
    "acct.low_level": ("A_level", "soft", "账号等级 ≤ Lv2"),
    "acct.lv6": ("A_lv6", "minus", "账号 Lv6"),
    "acct.official": ("A_official", "minus", "有官方认证"),
    "acct.vip": ("A_vip", "minus", "大会员"),
    "human.question": ("M_question", "minus", "简短提问"),
    "human.short_slang": ("M_slang", "minus", "简短口语 / 玩梗"),
    "human.drive_away": ("M_drive", "minus", "在驱赶 / 嘲讽机器人"),
    "cross.copy": ("C_copy", "soft", "与其他账号评论高度相似"),
    "cross.same_account_burst": ("C_burst", "soft", "同一账号短时间连发"),
    "cross.bot_chat": ("C_botchat", "soft", "与疑似机器人互相回复"),
    "cross.called_out": ("C_called", "soft", "被其他用户点名为机器人"),
}

#: 计算规则的权重。
COMPUTED_WEIGHTS = {
    "text.persona_phrase.strong": 12,
    "text.persona_phrase.weak": 5,
    "text.persona_phrase.cap": 35,
    "text.at_recommend": 20,
    "text.at_recommend.bonus": 10,
    "text.subtitle_summary": 15,
    "text.formal_punct": 6,
    "acct.low_level": 10,
    "acct.lv6": -10,
    "acct.official": -25,
    "acct.vip": -6,
    "human.question": -10,
    "human.short_slang": -12,
    "human.drive_away": -20,
    "cross.copy": 12,
    "cross.same_account_burst": 10,
    "cross.bot_chat": 12,
    "cross.called_out": 10,
    "cross.called_out.cap": 15,
}
