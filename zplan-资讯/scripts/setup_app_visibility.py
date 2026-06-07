#!/usr/bin/env python3
"""通过企微 API 查看和配置自建应用的可见范围与群聊权限。"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import requests

CORP_ID = os.getenv("WECHAT_CORP_ID", "").strip()
CORP_SECRET = os.getenv("WECHAT_CORP_SECRET", "").strip()
AGENT_ID = int(os.getenv("WECHAT_AGENT_ID", "0") or "0")

if not all([CORP_ID, CORP_SECRET, AGENT_ID]):
    print("❌ .env 中 WECHAT_CORP_ID / CORP_SECRET / AGENT_ID 未完整配置")
    sys.exit(1)

# 1. 获取 access_token
resp = requests.get(
    "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
    params={"corpid": CORP_ID, "corpsecret": CORP_SECRET},
    timeout=10,
)
data = resp.json()
if not data.get("access_token"):
    print(f"❌ 获取 token 失败: {data}")
    sys.exit(1)

token = data["access_token"]
print("✅ access_token 已获取")

# 2. 查看应用信息
resp = requests.get(
    "https://qyapi.weixin.qq.com/cgi-bin/agent/get",
    params={"access_token": token, "agentid": AGENT_ID},
    timeout=10,
)
info = resp.json()
if info.get("errcode") and info["errcode"] != 0:
    print(f"❌ 获取应用信息失败: {info}")
    sys.exit(1)

print(f"应用名称: {info.get('name', 'N/A')}")
print(f"允许群聊: {info.get('allow_groupchat', 'N/A')}")
print(f"可见范围(部门): {info.get('allow_partys', {}).get('partyid', [])}")
print(f"可见范围(用户): {info.get('allow_userinfos', {}).get('user', [])}")
print(f"可见范围(标签): {info.get('allow_tags', {}).get('tagid', [])}")

# 3. 如果 allow_groupchat 不为 true，提示开启
if not info.get("allow_groupchat"):
    print("\n⚠️ 群聊能力未开启！需要在企微后台开启")
    print("   路径: 应用管理 → 自建应用 → 功能 → 接收消息 → 开启群聊")

# 4. 如果可见范围为空，列出可用的部门
if not info.get("allow_partys", {}).get("partyid"):
    print("\n⚠️ 可见范围为空！尝试列出部门…")
    resp = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/department/list",
        params={"access_token": token},
        timeout=10,
    )
    depts = resp.json()
    if depts.get("department"):
        print("可用部门:")
        for d in depts["department"]:
            print(f"  ID={d['id']}  名称={d['name']}  parent={d.get('parentid', '-')}")

        # 尝试设置可见范围为根部门
        root_id = min(d["id"] for d in depts["department"])
        print(f"\n尝试将可见范围设为部门 {root_id}…")
        resp = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/agent/set?access_token={token}",
            json={
                "agentid": AGENT_ID,
                "allow_partys": {"partyid": [root_id]},
                "allow_groupchat": True,
            },
            timeout=10,
        )
        result = resp.json()
        if result.get("errcode") == 0:
            print("✅ 可见范围和群聊权限已设置！")
            print("   现在退出群聊重新进入，右上角 → 添加 → 搜索应用名称")
        else:
            print(f"❌ 设置失败: {result}")
            print("   可能需要超级管理员权限")
    else:
        print(f"❌ 无法获取部门列表: {depts}")
