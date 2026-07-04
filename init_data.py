#!/usr/bin/env python3
"""
init_data.py - 全量数据初始化
首次运行或每周日执行，拉取：
  - 目标板块成分股（含市值，用于识别龙头）
  - 每只股票 2023-01-01 至今的完整 K 线（用于计算参考低点和企稳判断）
  - 基本面数据（PE / PB / 市值）
  - 计算 2024.1 和 2024.8 两个参考低点

运行时间预计 30-90 分钟，取决于股票数量。
"""

import akshare as ak
import pandas as pd
import json
import time
import os
from datetime import date
from pathlib import Path

# ============================================================
# 目标板块（东方财富行业板块名称）
# ============================================================
TARGET_SECTORS = [
    # 消费
    "食品饮料", "白酒", "商贸零售", "家用电器", "纺织服装", "轻工制造",
    # 医药
    "医药生物", "医疗器械", "生物制品", "中药", "医疗服务",
    # 金融
    "银行", "证券", "保险", "多元金融",
    # 化工
    "基础化工", "农化制品",
    # 传媒 & 游戏
    "传媒", "游戏",
    # 农业 & 畜牧
    "农林牧渔",
    # 软件（PE 过滤自然去除 AI 泡沫）
    "计算机", "软件开发",
    # 高红利
    "公用事业", "电力",
    # 交通运输
    "交通运输", "航空机场", "港口航运",
    # 旅游 & 酒店
    "旅游及景区", "酒店餐饮",
    # 教育
    "教育",
    # 建筑建材
    "建筑材料",
    # 机械
    "工程机械",
    # 周期低位
    "煤炭", "钢铁",
    # 其他
    "环保", "通信",
]

# 排除的热门科技板块
EXCLUDE_SECTORS = [
    "半导体", "芯片", "算力", "人工智能", "机器人",
    "光伏设备", "风电", "新能源", "储能", "消费电子",
    "卫星导航", "量子信息",
]

# ============================================================
# 路径配置
# ============================================================
DATA_DIR = Path("data")
HISTORY_DIR = DATA_DIR / "history"
FUND_DIR = DATA_DIR / "fundamentals"

SECTOR_STOCKS_FILE = DATA_DIR / "sector_stocks.csv"
REFERENCE_LOWS_FILE = DATA_DIR / "reference_lows.csv"
ALERT_STATE_FILE = DATA_DIR / "alert_state.json"

HISTORY_START = "20230101"

# 参考低点的时间窗口
LOW_2024_01_START = "2024-01-01"
LOW_2024_01_END   = "2024-03-01"
LOW_2024_08_START = "2024-07-15"
LOW_2024_08_END   = "2024-09-30"

# API 调用间隔（秒），避免被封
API_DELAY = 0.5


def init_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(exist_ok=True)
    FUND_DIR.mkdir(exist_ok=True)


# ============================================================
# Step 1: 获取板块成分股
# ============================================================
def fetch_sector_stocks() -> pd.DataFrame:
    print("\n[Step 1] 获取板块成分股...")

    # 先获取需要排除的股票代码
    exclude_codes: set[str] = set()
    for sector in EXCLUDE_SECTORS:
        try:
            df = ak.stock_board_industry_cons_em(symbol=sector)
            if df is not None and not df.empty:
                exclude_codes.update(df["代码"].astype(str).str.zfill(6).tolist())
        except Exception as e:
            print(f"  [warn] 排除板块 {sector}: {e}")
        time.sleep(API_DELAY)
    print(f"  排除热门科技股票 {len(exclude_codes)} 只")

    # 获取目标板块
    frames = []
    for sector in TARGET_SECTORS:
        try:
            df = ak.stock_board_industry_cons_em(symbol=sector)
            if df is None or df.empty:
                print(f"  [warn] {sector}: 无数据")
                continue
            df = df.copy()
            df["sector"] = sector
            frames.append(df)
            print(f"  {sector}: {len(df)} 只")
        except Exception as e:
            print(f"  [warn] {sector}: {e}")
        time.sleep(API_DELAY)

    if not frames:
        raise RuntimeError("所有目标板块数据获取失败")

    combined = pd.concat(frames, ignore_index=True)
    combined["代码"] = combined["代码"].astype(str).str.zfill(6)

    # 去除排除板块的股票
    combined = combined[~combined["代码"].isin(exclude_codes)].copy()

    # 标准化列名
    rename = {
        "代码": "code",
        "名称": "name",
        "总市值": "market_cap",
        "市盈率-动态": "pe",
        "市净率": "pb",
        "涨跌幅": "change_pct",
    }
    combined = combined.rename(columns={k: v for k, v in rename.items() if k in combined.columns})

    # 市值转为亿元
    if "market_cap" in combined.columns:
        combined["market_cap"] = pd.to_numeric(combined["market_cap"], errors="coerce") / 1e8

    # 去重：一只股票可能属于多个板块，保留市值最大的那个板块记录
    combined = combined.sort_values("market_cap", ascending=False).drop_duplicates(subset="code")

    print(f"\n  目标股票总计（去重去排除）: {len(combined)} 只")
    return combined[["code", "name", "sector", "market_cap", "pe", "pb"]].reset_index(drop=True)


# ============================================================
# Step 2: 拉取历史 K 线
# ============================================================
def fetch_history_kline(code: str) -> pd.DataFrame | None:
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=HISTORY_START,
            end_date=date.today().strftime("%Y%m%d"),
            adjust="qfq",
        )
        if df is None or df.empty:
            return None
        # 统一列名
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low",
            "成交量": "volume", "成交额": "amount",
            "涨跌幅": "pct_change", "涨跌额": "change",
            "换手率": "turnover",
        })
        df["date"] = df["date"].astype(str)
        return df
    except Exception as e:
        print(f"    [warn] K线 {code}: {e}")
        return None


# ============================================================
# Step 3: 计算参考低点
# ============================================================
def compute_reference_lows(df: pd.DataFrame, code: str, name: str) -> dict:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    def period_low(start: str, end: str):
        mask = (df["date"] >= start) & (df["date"] <= end)
        sub = df[mask]
        if sub.empty:
            return None, None
        idx = sub["low"].idxmin()
        return float(sub.loc[idx, "low"]), str(sub.loc[idx, "date"].date())

    low_01, date_01 = period_low(LOW_2024_01_START, LOW_2024_01_END)
    low_08, date_08 = period_low(LOW_2024_08_START, LOW_2024_08_END)

    return {
        "code": code,
        "name": name,
        "low_2024_01": low_01,
        "low_2024_01_date": date_01,
        "low_2024_08": low_08,
        "low_2024_08_date": date_08,
        # 取两者最低值作为"历史地板"
        "floor_price": min(
            x for x in [low_01, low_08] if x is not None
        ) if any(x is not None for x in [low_01, low_08]) else None,
    }


# ============================================================
# Step 4: 获取基本面数据（PE / PB / 市值）
# ============================================================
def fetch_fundamentals(code: str, name: str) -> dict:
    result = {"code": code, "name": name, "last_updated": date.today().isoformat()}
    try:
        info = ak.stock_individual_info_em(symbol=code)
        if info is not None and not info.empty:
            # info 是两列 DataFrame：item | value
            cols = info.columns.tolist()
            kv = dict(zip(info.iloc[:, 0], info.iloc[:, 1]))
            result.update({
                "pe": kv.get("市盈率(动)") or kv.get("市盈率TTM"),
                "pb": kv.get("市净率"),
                "roe": kv.get("净资产收益率"),
                "market_cap_yi": kv.get("总市值"),
                "industry": kv.get("行业"),
            })
    except Exception as e:
        result["error"] = str(e)
    return result


# ============================================================
# 主函数
# ============================================================
def main():
    print("=" * 60)
    print("  板块龙头追踪系统 - 数据初始化")
    print("=" * 60)

    init_dirs()

    # ── Step 1: 板块成分股 ──────────────────────────────────
    stocks = fetch_sector_stocks()
    stocks.to_csv(SECTOR_STOCKS_FILE, index=False, encoding="utf-8-sig")
    print(f"  已保存 → {SECTOR_STOCKS_FILE}")

    # ── Step 2 & 3: K线 + 参考低点 ─────────────────────────
    print(f"\n[Step 2&3] 拉取历史K线并计算参考低点 (共 {len(stocks)} 只)...")

    reference_lows = []
    total = len(stocks)

    for i, row in stocks.iterrows():
        code = row["code"]
        name = row["name"]
        hist_path = HISTORY_DIR / f"{code}.csv"

        # 如果已有历史文件，跳过重新拉取（增量模式）
        if hist_path.exists():
            df = pd.read_csv(hist_path)
            print(f"  [{i+1}/{total}] {code} {name} - 已有缓存，跳过")
        else:
            print(f"  [{i+1}/{total}] {code} {name}...", end=" ", flush=True)
            df = fetch_history_kline(code)
            if df is not None and not df.empty:
                df.to_csv(hist_path, index=False, encoding="utf-8-sig")
                print(f"OK ({len(df)} 条)")
            else:
                print("无数据，跳过")
                time.sleep(API_DELAY)
                continue
            time.sleep(API_DELAY)

        lows = compute_reference_lows(df, code, name)
        reference_lows.append(lows)

    lows_df = pd.DataFrame(reference_lows)
    lows_df.to_csv(REFERENCE_LOWS_FILE, index=False, encoding="utf-8-sig")
    print(f"\n  参考低点已保存 → {REFERENCE_LOWS_FILE}")

    # ── Step 4: 基本面数据 ──────────────────────────────────
    print(f"\n[Step 4] 获取基本面数据...")
    total = len(stocks)
    for i, row in stocks.iterrows():
        code = row["code"]
        name = row["name"]
        fund_path = FUND_DIR / f"{code}.json"

        if fund_path.exists():
            continue  # 已有，跳过

        print(f"  [{i+1}/{total}] {code} {name}...", end=" ", flush=True)
        fund = fetch_fundamentals(code, name)
        with open(fund_path, "w", encoding="utf-8") as f:
            json.dump(fund, f, ensure_ascii=False, indent=2)
        print("OK")
        time.sleep(API_DELAY)

    # ── Step 5: 初始化 alert_state ──────────────────────────
    if not ALERT_STATE_FILE.exists():
        with open(ALERT_STATE_FILE, "w") as f:
            json.dump({}, f)
        print(f"\n  alert_state 已初始化 → {ALERT_STATE_FILE}")

    print("\n" + "=" * 60)
    print(f"  初始化完成！共处理 {len(stocks)} 只股票")
    print("=" * 60)


if __name__ == "__main__":
    main()
