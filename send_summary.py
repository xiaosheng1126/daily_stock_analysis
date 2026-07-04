#!/usr/bin/env python3
"""
send_summary.py - 每日固定推送汇总报告
读取 screening_results.json，生成三类信号汇总消息，
直接推送到飞书/企业微信/Telegram（不经过 DSA，确保每天固定有消息）。

有信号时：列出今日三类信号，提示去看 DSA 深度分析报告
无信号时：推送「今日无新信号」简报
"""

import json
import os
import requests
from datetime import date
from pathlib import Path

RESULTS_FILE = Path("screening_results.json")

# ============================================================
# 消息格式化
# ============================================================
def format_message(data: dict) -> str:
    today_str = data.get("date", date.today().strftime("%Y-%m-%d"))
    results = data.get("results", {})
    new_lows = results.get("new_low", [])
    stabilizing = results.get("stabilizing", [])
    potential = results.get("potential", [])
    total = data.get("total_signals", 0)

    lines = [
        f"📊 **板块龙头追踪日报 · {today_str}**",
        "",
    ]

    # 🔴 创新低
    if new_lows:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🔴 **创新低预警** ({len(new_lows)} 只)")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for s in new_lows:
            lines.append(f"· **{s['name']}**({s['code']}) [{s['sector']}]")
            lines.append(f"  {s['detail']}")
            lines.append(f"  当前价: {s['current_price']:.2f}")
        lines.append("")
    else:
        lines.append("🔴 今日无新低预警")

    # 🟡 企稳
    if stabilizing:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🟡 **震荡企稳信号** ({len(stabilizing)} 只)")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for s in stabilizing:
            lines.append(f"· **{s['name']}**({s['code']}) [{s['sector']}]")
            lines.append(f"  位置: {s['low_level']} | 参考地板: {s['floor_price']:.2f}")
            lines.append(f"  信号: {s['detail']}")
        lines.append("")
    else:
        lines.append("🟡 今日无企稳信号")

    # 🟢 低位龙头候选
    if potential:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🟢 **低位龙头候选** ({len(potential)} 只，DSA 正在评估催化剂)")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for s in potential:
            stab_tag = " ★已企稳" if s.get("is_stabilizing") else ""
            cap = f" | 市值{s['market_cap']:.0f}亿" if s.get("market_cap") else ""
            lines.append(f"· **{s['name']}**({s['code']}) [{s['sector']}]{stab_tag}")
            lines.append(f"  {s['low_level']} | 参考地板: {s['floor_price']:.2f}{cap}")
        lines.append("")
    else:
        lines.append("🟢 今日无新龙头候选")

    lines.append("")
    if total > 0:
        lines.append(f"📋 共 {total} 只股票进入 DSA 深度分析，详细买卖评分请见后续报告")
    else:
        lines.append("📋 今日所有板块无新信号，市场平静")

    lines.append("")
    lines.append("_数据来源：A股行情 · 板块龙头追踪系统_")

    return "\n".join(lines)


def format_feishu_card(data: dict) -> dict:
    """飞书卡片格式（比纯文本更好看）"""
    msg = format_message(data)
    return {
        "msg_type": "text",
        "content": {"text": msg},
    }


# ============================================================
# 推送渠道
# ============================================================
def send_feishu(webhook_url: str, message: str) -> bool:
    try:
        payload = {"msg_type": "text", "content": {"text": message}}
        r = requests.post(webhook_url, json=payload, timeout=15)
        if r.status_code == 200:
            resp = r.json()
            if resp.get("code") == 0 or resp.get("StatusCode") == 0:
                print("  飞书推送成功")
                return True
        print(f"  飞书推送失败: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  飞书推送异常: {e}")
    return False


def send_wechat(webhook_url: str, message: str) -> bool:
    try:
        payload = {"msgtype": "markdown", "markdown": {"content": message}}
        r = requests.post(webhook_url, json=payload, timeout=15)
        if r.status_code == 200 and r.json().get("errcode") == 0:
            print("  企业微信推送成功")
            return True
        print(f"  企业微信推送失败: {r.text[:200]}")
    except Exception as e:
        print(f"  企业微信推送异常: {e}")
    return False


def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200 and r.json().get("ok"):
            print("  Telegram 推送成功")
            return True
        print(f"  Telegram 推送失败: {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram 推送异常: {e}")
    return False


# ============================================================
# 主函数
# ============================================================
def main():
    print("=" * 50)
    print("  每日汇总推送")
    print("=" * 50)

    # 读取筛选结果
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        # 没有结果文件，构造空报告
        data = {
            "date": date.today().strftime("%Y-%m-%d"),
            "total_signals": 0,
            "results": {"new_low": [], "stabilizing": [], "potential": []},
        }
        print("  未找到 screening_results.json，发送空报告")

    message = format_message(data)
    print("\n--- 消息预览 ---")
    print(message[:500])
    print("...")

    # 读取环境变量推送渠道
    sent = False

    feishu_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    if feishu_url:
        sent |= send_feishu(feishu_url, message)

    wechat_url = os.getenv("WECHAT_WEBHOOK_URL", "")
    if wechat_url:
        sent |= send_wechat(wechat_url, message)

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        sent |= send_telegram(tg_token, tg_chat, message)

    if not sent:
        print("  WARNING: 未配置任何推送渠道，消息未发送")
        print("  请设置 FEISHU_WEBHOOK_URL / WECHAT_WEBHOOK_URL / TELEGRAM_BOT_TOKEN")


if __name__ == "__main__":
    main()
