#!/usr/bin/env python3
"""
daily_screen.py - 每日筛选
读取 data/ 缓存，拉今日行情追加历史，检测三类信号：
  🔴 创新低：今日价格首次跌破 2024.1 或 2024.8 参考低点
  🟡 企稳：技术指标满足企稳条件（均线/量能/振幅收窄）
  🟢 低位龙头：板块市值前2、处于低位区间，交给 DSA 评估催化剂

输出：
  screening_results.json  - 三类信号详情（供 send_summary.py 使用）
  screened_stocks.txt     - 待 DSA 分析的股票代码列表
  GITHUB_ENV              - 写入 SCREENED_STOCK_LIST 供 workflow 读取
"""

import akshare as ak
import pandas as pd
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

# ============================================================
# 路径
# ============================================================
DATA_DIR = Path("data")
HISTORY_DIR = DATA_DIR / "history"
FUND_DIR = DATA_DIR / "fundamentals"

SECTOR_STOCKS_FILE = DATA_DIR / "sector_stocks.csv"
REFERENCE_LOWS_FILE = DATA_DIR / "reference_lows.csv"
ALERT_STATE_FILE = DATA_DIR / "alert_state.json"
RESULTS_FILE = Path("screening_results.json")
STOCKS_FILE = Path("screened_stocks.txt")

# ============================================================
# 低位判断阈值
# ============================================================
LOW_EXTREME = 1.15   # 极低位：当前价 ≤ 参考低点 × 1.15
LOW_NORMAL  = 1.30   # 低位区：当前价 ≤ 参考低点 × 1.30

# 企稳参数
MA_SHORT      = 5
MA_LONG       = 20
VOL_RATIO     = 0.85   # 近5日量 / 近20日量 < 0.85 视为缩量
AMP_RATIO     = 0.70   # 近20日振幅 / 前20日振幅 < 0.70 视为收窄
SIGNAL_NEEDED = 3      # 满足 N 个企稳信号才触发

# 企稳信号重复提醒间隔（天）
STABILIZE_COOLDOWN_DAYS = 5

# 每板块取市值前 N 名作为龙头候选
TOP_PER_SECTOR = 2


# ============================================================
# 数据加载
# ============================================================
def load_sector_stocks() -> pd.DataFrame:
    df = pd.read_csv(SECTOR_STOCKS_FILE, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    return df


def load_reference_lows() -> dict:
    df = pd.read_csv(REFERENCE_LOWS_FILE, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    return df.set_index("code").to_dict("index")


def load_alert_state() -> dict:
    if ALERT_STATE_FILE.exists():
        with open(ALERT_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_alert_state(state: dict):
    with open(ALERT_STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_history(code: str) -> pd.DataFrame:
    path = HISTORY_DIR / f"{code}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"date": str})
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def append_today_to_history(code: str, today_row: dict) -> pd.DataFrame:
    hist = load_history(code)
    today_dt = pd.to_datetime(today_row["date"])

    # 已有今日数据则直接返回
    if not hist.empty and today_dt in hist["date"].values:
        return hist

    new_row = pd.DataFrame([today_row])
    new_row["date"] = pd.to_datetime(new_row["date"])
    hist = pd.concat([hist, new_row], ignore_index=True).sort_values("date").reset_index(drop=True)

    if not hist.empty:
        hist.to_csv(HISTORY_DIR / f"{code}.csv", index=False, encoding="utf-8-sig")

    return hist


# ============================================================
# 信号检测
# ============================================================
def check_new_low(
    hist: pd.DataFrame,
    today_low: float,
    ref_low_01: float | None,
    ref_low_08: float | None,
    alert_state: dict,
    code: str,
    today_str: str,
) -> list[dict]:
    """检测是否创新低，返回触发的信号列表（可能同时触发两个）"""
    triggered = []

    checks = [
        ("new_low_2024_01", ref_low_01, "2024.1低点"),
        ("new_low_2024_08", ref_low_08, "2024.8低点"),
    ]

    for key_suffix, ref_low, label in checks:
        if ref_low is None or pd.isna(ref_low):
            continue
        if today_low < ref_low:
            state_key = f"{code}_{key_suffix}"
            prev = alert_state.get(state_key, {})

            # 只在首次跌破时提醒；若之前触发过，需要反弹超过5%才重置
            if prev.get("triggered"):
                # 检查是否反弹过（反弹 > 5% 后再次跌破才重新提醒）
                reset_price = prev.get("reset_price", 0)
                if reset_price and today_low > reset_price * 1.05:
                    pass  # 反弹过了，本次可以重新触发
                else:
                    continue  # 尚未反弹，不重复提醒

            triggered.append({
                "type": key_suffix,
                "label": label,
                "ref_low": ref_low,
                "today_low": today_low,
                "detail": f"今日低点 {today_low:.2f} 跌破{label} {ref_low:.2f}",
            })
            alert_state[state_key] = {
                "triggered": True,
                "date": today_str,
                "price": today_low,
                "reset_price": today_low,  # 用于跟踪后续反弹
            }

    return triggered


def check_stabilizing(hist: pd.DataFrame) -> dict:
    """检测企稳信号，返回触发详情"""
    result = {"triggered": False, "score": 0, "signals": [], "detail": ""}

    if len(hist) < MA_LONG + 5:
        return result

    close = hist["close"].values if "close" in hist.columns else hist["收盘"].values
    high = hist["high"].values if "high" in hist.columns else hist["最高"].values
    low = hist["low"].values if "low" in hist.columns else hist["最低"].values

    vol_col = "volume" if "volume" in hist.columns else "成交量"

    signals = []

    # 信号1: MA5 > MA20（均线金叉）
    ma5 = pd.Series(close).rolling(MA_SHORT).mean().values
    ma20 = pd.Series(close).rolling(MA_LONG).mean().values
    if ma5[-1] > ma20[-1]:
        signals.append("MA5>MA20")

    # 信号2: MA20 斜率趋平或向上（近5天斜率 >= -1%）
    if ma20[-1] > 0 and ma20[-6] > 0:
        slope = (ma20[-1] - ma20[-6]) / ma20[-6]
        if slope >= -0.01:
            signals.append("均线趋稳")

    # 信号3: 振幅收窄
    if len(hist) >= 40:
        recent_amp_pct = (
            max(high[-20:]) - min(low[-20:])
        ) / min(low[-20:]) if min(low[-20:]) > 0 else 0
        prev_amp_pct = (
            max(high[-40:-20]) - min(low[-40:-20])
        ) / min(low[-40:-20]) if min(low[-40:-20]) > 0 else 0
        if prev_amp_pct > 0 and recent_amp_pct < prev_amp_pct * AMP_RATIO:
            signals.append("振幅收窄")

    # 信号4: 近5日缩量
    if vol_col in hist.columns:
        vol = hist[vol_col].values
        if len(vol) >= MA_LONG:
            vol5 = vol[-5:].mean()
            vol20 = vol[-20:].mean()
            if vol20 > 0 and vol5 < vol20 * VOL_RATIO:
                signals.append("缩量整理")

    # 信号5: 价格在 MA20 之上
    if close[-1] > ma20[-1]:
        signals.append("站上MA20")

    result["score"] = len(signals)
    result["signals"] = signals

    if len(signals) >= SIGNAL_NEEDED:
        result["triggered"] = True
        result["detail"] = " / ".join(signals)

    return result


def is_in_low_zone(
    current_price: float,
    ref_low_01: float | None,
    ref_low_08: float | None,
) -> tuple[bool, str | None, float | None]:
    """判断是否在低位区间，返回 (in_zone, level, floor_price)"""
    candidates = [x for x in [ref_low_01, ref_low_08] if x is not None and not pd.isna(x)]
    if not candidates:
        return False, None, None

    floor = min(candidates)
    ratio = current_price / floor

    if ratio <= LOW_EXTREME:
        return True, "极低位", floor
    elif ratio <= LOW_NORMAL:
        return True, "低位区间", floor
    return False, None, None


# ============================================================
# 主函数
# ============================================================
def screen() -> list[str]:
    today_str = date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  板块龙头追踪系统 - 每日筛选  {today_str}")
    print(f"{'='*60}\n")

    # 加载缓存数据
    sector_stocks = load_sector_stocks()
    ref_lows_dict = load_reference_lows()
    alert_state = load_alert_state()

    print(f"加载缓存：{len(sector_stocks)} 只目标股票\n")

    # 获取今日全市场行情（一次批量调用）
    print("拉取今日行情（全市场快照）...")
    try:
        snapshot = ak.stock_zh_a_spot_em()
        snapshot["代码"] = snapshot["代码"].astype(str).str.zfill(6)
        snap_dict = snapshot.set_index("代码").to_dict("index")
        print(f"获取行情 {len(snap_dict)} 只\n")
    except Exception as e:
        print(f"行情获取失败: {e}")
        return []

    # 获取板块龙头列表（每板块市值前 TOP_PER_SECTOR）
    leader_codes: set[str] = set()
    for sector in sector_stocks["sector"].unique():
        top = (
            sector_stocks[sector_stocks["sector"] == sector]
            .sort_values("market_cap", ascending=False)
            .head(TOP_PER_SECTOR)["code"]
            .tolist()
        )
        leader_codes.update(top)

    results = {"new_low": [], "stabilizing": [], "potential": []}
    today_signals: set[str] = set()  # 汇总需要 DSA 分析的股票

    stocks_to_check = sector_stocks.drop_duplicates("code")
    total = len(stocks_to_check)

    for i, row in stocks_to_check.iterrows():
        code = row["code"]
        name = row["name"]
        sector = row["sector"]

        today_snap = snap_dict.get(code)
        if not today_snap:
            continue

        # 映射今日数据到统一格式
        current_price = float(today_snap.get("最新价", 0) or 0)
        today_row = {
            "date": today_str,
            "open": float(today_snap.get("今开", 0) or 0),
            "high": float(today_snap.get("最高", 0) or 0),
            "low": float(today_snap.get("最低", 0) or 0),
            "close": current_price,
            "volume": float(today_snap.get("成交量", 0) or 0),
            "amount": float(today_snap.get("成交额", 0) or 0),
            "pct_change": float(today_snap.get("涨跌幅", 0) or 0),
        }

        # 追加今日数据到历史
        hist = append_today_to_history(code, today_row)

        # 获取参考低点
        ref = ref_lows_dict.get(code, {})
        ref_low_01 = ref.get("low_2024_01")
        ref_low_08 = ref.get("low_2024_08")

        # ── 🔴 创新低检测 ──────────────────────────────────
        new_lows = check_new_low(
            hist, today_row["low"], ref_low_01, ref_low_08,
            alert_state, code, today_str,
        )
        for nl in new_lows:
            results["new_low"].append({
                "code": code, "name": name, "sector": sector,
                "current_price": current_price,
                "ref_low": nl["ref_low"],
                "today_low": nl["today_low"],
                "label": nl["label"],
                "detail": nl["detail"],
            })
            today_signals.add(code)

        # ── 🟡 企稳检测（只在低位区间内才检测）────────────
        in_zone, low_level, floor_price = is_in_low_zone(current_price, ref_low_01, ref_low_08)

        if in_zone and len(hist) >= MA_LONG + 5:
            stab = check_stabilizing(hist)
            if stab["triggered"]:
                state_key = f"{code}_stabilizing"
                prev = alert_state.get(state_key, {})
                last_date = prev.get("date")
                days_since = (
                    (pd.Timestamp(today_str) - pd.Timestamp(last_date)).days
                    if last_date else 999
                )
                if days_since >= STABILIZE_COOLDOWN_DAYS:
                    results["stabilizing"].append({
                        "code": code, "name": name, "sector": sector,
                        "current_price": current_price,
                        "low_level": low_level,
                        "floor_price": floor_price,
                        "detail": stab["detail"],
                        "score": stab["score"],
                    })
                    alert_state[state_key] = {"triggered": True, "date": today_str}
                    today_signals.add(code)

        # ── 🟢 低位龙头候选（板块前N + 低位区间）─────────
        if code in leader_codes and in_zone:
            results["potential"].append({
                "code": code, "name": name, "sector": sector,
                "current_price": current_price,
                "low_level": low_level,
                "floor_price": floor_price,
                "is_stabilizing": stab["triggered"] if in_zone and len(hist) >= MA_LONG + 5 else False,
                "market_cap": float(row.get("market_cap", 0) or 0),
            })
            today_signals.add(code)

    # 保存 alert_state
    save_alert_state(alert_state)

    # ── 输出摘要 ────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  筛选结果")
    print(f"{'='*60}")
    print(f"🔴 创新低预警: {len(results['new_low'])} 只")
    for s in results["new_low"]:
        print(f"   {s['code']} {s['name']:8s} {s['detail']}")
    print(f"\n🟡 震荡企稳: {len(results['stabilizing'])} 只")
    for s in results["stabilizing"]:
        print(f"   {s['code']} {s['name']:8s} [{s['low_level']}] {s['detail']}")
    print(f"\n🟢 低位龙头候选: {len(results['potential'])} 只")
    for s in results["potential"]:
        stab_mark = " ★企稳" if s["is_stabilizing"] else ""
        print(f"   {s['code']} {s['name']:8s} [{s['sector']}] {s['low_level']}{stab_mark}")

    stock_list = ",".join(sorted(today_signals))
    print(f"\n共需 DSA 分析: {len(today_signals)} 只")
    print(f"STOCK_LIST: {stock_list or '(今日无信号)'}")

    # 保存结果文件
    output = {
        "date": today_str,
        "total_signals": len(today_signals),
        "results": results,
    }
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    with open(STOCKS_FILE, "w") as f:
        f.write(stock_list)

    # 写入 GitHub Actions 环境变量
    genv = os.getenv("GITHUB_ENV", "")
    if genv:
        with open(genv, "a") as f:
            f.write(f"SCREENED_STOCK_LIST={stock_list}\n")

    return list(today_signals)


if __name__ == "__main__":
    codes = screen()
    print(f"\n完成，共 {len(codes)} 只股票待分析")
