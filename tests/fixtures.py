"""测试人设中心 —— 全部合成数据，跨套件统一取用。

规则（2026-10-10 与维护者确认）：
- 4 个联系人 + 2 个群 + 1 个公众号，中性命名，不带「老师/同学」类语义标签；
- 需要人设的夹具（make_account / stats / sns 账号）一律从这里取常量；
- 纯函数边界测试里的形状字面量（wxid_a_b_1234、wxid_with_xxx 等）测的是
  解析规则本身，留在用例本地，不算人设。
"""

# 库主账号（导出/统计/sns 的 account 主体）
DEMO_SELF = "wxid_demo_a"

# 联系人：ID 与显示名按下标一一对应
DEMO_IDS = ("wxid_demo_b", "wxid_demo_c", "wxid_demo_d")
DEMO_NAMES = ("联系人B", "联系人C", "联系人D")

# 群聊（合成 ID 用字母段，避免撞真实数字群号形态的标识扫描规则）
DEMO_GROUP_IDS = ("demogroup01@chatroom", "demogroup02@chatroom")
DEMO_GROUP_NAMES = ("群聊A", "群聊B")

# 公众号
DEMO_GH_ID = "gh_demo01"
DEMO_GH_NAME = "公众号A"
