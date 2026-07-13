#!/usr/bin/env python3
"""TDXQuant MCP Server - 通达信量化接口 MCP 服务器。

基于 easy_tdx，通过通达信原生协议直连行情服务器，无需启动客户端。
提供实时行情、历史K线、板块数据、资金流向等量化数据接口。
"""
from __future__ import annotations

import datetime
import json
import sys

try:
    from fastmcp import FastMCP
except ImportError:
    print("错误: 未安装 fastmcp，请运行: pip install fastmcp", file=sys.stderr)
    sys.exit(1)

try:
    from easy_tdx import (
        UnifiedTdxClient, Period, BoardType, Adjust,
        SortType, SortOrder, Category, FilterType,
    )
except ImportError:
    print("错误: 未安装 easy_tdx，请运行: pip install easy_tdx", file=sys.stderr)
    sys.exit(1)

mcp = FastMCP("tdxquant")

# ──────────────────────── 客户端管理 ────────────────────────

_client: UnifiedTdxClient | None = None


def _get_client() -> UnifiedTdxClient:
    global _client
    if _client is None:
        _client = UnifiedTdxClient()
    return _client


def _ensure_connected(max_retries: int = 2):
    """确保客户端已连接，断线自动重连。"""
    client = _get_client()
    for attempt in range(max_retries):
        try:
            client.get_server_info()
            return
        except Exception:
            try:
                client.connect()
            except Exception:
                if attempt == max_retries - 1:
                    raise


# ──────────────────────── 通用工具 ────────────────────────

def _df_to_records(df, float_round: int = 4):
    """DataFrame → list[dict]，处理 datetime/date/time/float 序列化。"""
    if df is None or df.empty:
        return []
    df = df.copy()
    for col in df.columns:
        dtype = str(df[col].dtype)
        if "datetime64" in dtype:
            df[col] = df[col].astype(str)
        elif df[col].dtype in ("float32", "float64"):
            df[col] = df[col].round(float_round)
        else:
            # 逐值检查 date/time 对象
            df[col] = df[col].apply(
                lambda v: v.strftime("%Y-%m-%d") if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime)
                else v.strftime("%H:%M:%S") if isinstance(v, datetime.time)
                else str(v) if isinstance(v, datetime.datetime)
                else v
            )
    return df.to_dict("records")


def _ok(data: dict, **extra) -> str:
    """构造成功响应。"""
    result = {"ok": True, **extra, "count": len(data) if isinstance(data, list) else 1, "data": data}
    return json.dumps(result, ensure_ascii=False)


def _err(e: Exception) -> str:
    """构造失败响应。"""
    return json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}, ensure_ascii=False)


def _convert_symbol(symbol: str) -> tuple[int, str]:
    """股票代码 → (market, code)。支持 600519 / sh600519 / 600519.SH 等格式。"""
    s = symbol.strip()
    su = s.upper()
    if su.endswith(".SH"):
        return 1, su[:-3]
    if su.endswith(".SZ"):
        return 0, su[:-3]
    sl = s.lower()
    if sl.startswith("sh"):
        return 1, s[2:]
    if sl.startswith("sz"):
        return 0, s[2:]
    if len(s) == 6 and s.isdigit():
        if s[0] == "6" or s[:2] in ("68", "90", "51", "11", "13"):
            return 1, s
        return 0, s
    return 1, s


# ── 枚举映射 ──

_PERIOD_MAP = {
    "1min": Period.MIN_1, "5min": Period.MIN_5, "15min": Period.MIN_15,
    "30min": Period.MIN_30, "60min": Period.MIN_60, "day": Period.DAILY,
    "week": Period.WEEKLY, "month": Period.MONTHLY,
    "quarter": Period.QUARTERLY, "year": Period.YEARLY,
}

_BOARD_TYPE_MAP = {
    "industry": BoardType.HY, "industry2": BoardType.HY2,
    "concept": BoardType.GN, "style": BoardType.FG,
    "region": BoardType.DQ, "all": BoardType.ALL,
}

_ADJUST_MAP = {"none": Adjust.NONE, "qfq": Adjust.QFQ, "hfq": Adjust.HFQ}

_SORT_TYPE_MAP = {
    "change_pct": SortType.CHANGE_PCT, "price": SortType.PRICE,
    "volume": SortType.VOLUME, "amount": SortType.TOTAL_AMOUNT, "code": SortType.CODE,
}

_SORT_ORDER_MAP = {"asc": SortOrder.ASC, "desc": SortOrder.DESC, "none": SortOrder.NONE}

_CATEGORY_MAP = {
    "sh": Category.SH, "sz": Category.SZ, "a": Category.A, "b": Category.B,
    "kcb": Category.KCB, "bj": Category.BJ, "cyb": Category.CYB,
    "etf": Category.ETF, "lof": Category.LOF,
    "hgt": Category.HGT, "sgt": Category.SGT,
    "board_hy": Category.BOARD_HY, "board_gn": Category.BOARD_GN,
}

# 板块代码缓存: name → code
_board_name_cache: dict[str, str] = {}


def _resolve_board_symbol(board_symbol: str) -> str:
    """将板块代码/名称统一解析为数字代码。"""
    if board_symbol.isdigit():
        return board_symbol
    # 尝试缓存
    if board_symbol in _board_name_cache:
        return _board_name_cache[board_symbol]
    # 拉取板块列表构建缓存
    try:
        _ensure_connected()
        client = _get_client()
        for bt in (BoardType.HY, BoardType.GN, BoardType.FG, BoardType.DQ):
            df = client.get_board_list(board_type=bt, count=10000)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    _board_name_cache[str(row["name"])] = str(row["code"])
        if board_symbol in _board_name_cache:
            return _board_name_cache[board_symbol]
    except Exception:
        pass
    return board_symbol


# ──────────────────────── MCP 工具 ────────────────────────

@mcp.tool(description="连接通达信行情服务器并测试连通性")
def connect_test() -> str:
    """连接通达信行情服务器，返回服务器信息。"""
    try:
        client = _get_client()
        client.connect()
        info = _df_to_records(client.get_server_info())
        return _ok(info, connected=True)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取股票实时行情")
def get_stock_quotes(symbol: str) -> str:
    """获取单只股票实时行情。

    Args:
        symbol: 股票代码，如 600519 / sh600519 / 600519.SH
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        df = client.get_stock_quotes(stocks=[(market, code)])
        data = _df_to_records(df)
        if data:
            data[0]["market_name"] = "上海" if market == 1 else "深圳"
        return _ok(data, symbol=symbol)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取股票历史K线")
def get_stock_kline(symbol: str, period: str = "day", count: int = 100, adjust: str = "none") -> str:
    """获取股票历史K线数据。

    Args:
        symbol: 股票代码
        period: K线周期 - 1min/5min/15min/30min/60min/day/week/month/quarter/year
        count: 获取条数，默认100
        adjust: 复权方式 - none/qfq/hfq
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        df = client.get_stock_kline(
            market=market, code=code,
            period=_PERIOD_MAP.get(period, Period.DAILY),
            count=count,
            adjust=_ADJUST_MAP.get(adjust, Adjust.NONE),
        )
        return _ok(_df_to_records(df), symbol=symbol, period=period, adjust=adjust)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取股票K线（含技术指标）")
def get_stock_kline_with_indicators(symbol: str, indicators: str = "MACD", period: str = "day", count: int = 30, adjust: str = "qfq") -> str:
    """获取带技术指标的K线数据。

    Args:
        symbol: 股票代码
        indicators: 技术指标，逗号分隔，如 MACD,KDJ,RSI,BOLL,MA
        period: K线周期，默认 day
        count: 获取条数，默认30
        adjust: 复权方式 - none/qfq/hfq，默认 qfq
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        ind_list = [i.strip().upper() for i in indicators.split(",")]
        df = client.get_stock_kline_with_indicators(
            market=market, code=code, indicators=ind_list,
            period=_PERIOD_MAP.get(period, Period.DAILY),
            count=count,
            adjust=_ADJUST_MAP.get(adjust, Adjust.QFQ),
        )
        return _ok(_df_to_records(df), symbol=symbol, indicators=ind_list, period=period)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取板块列表")
def get_board_list(board_type: str = "all", count: int = 1000) -> str:
    """获取板块列表。

    Args:
        board_type: 板块类型 - industry(行业)/industry2(行业2)/concept(概念)/style(风格)/region(地域)/all(全部)
        count: 获取数量，默认1000
    """
    try:
        _ensure_connected()
        client = _get_client()
        df = client.get_board_list(board_type=_BOARD_TYPE_MAP.get(board_type, BoardType.ALL), count=count)
        data = _df_to_records(df)
        # 同步更新板块名称缓存
        for item in data:
            if "name" in item and "code" in item:
                _board_name_cache[str(item["name"])] = str(item["code"])
        return _ok(data, board_type=board_type)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取板块成分股")
def get_board_members(board_symbol: str, count: int = 1000, sort_by: str = "change_pct", sort_order: str = "desc") -> str:
    """获取板块成分股列表。支持板块代码(如881007)或名称(如油气开采)。

    Args:
        board_symbol: 板块代码或名称
        count: 获取数量，默认1000
        sort_by: 排序字段 - change_pct/price/volume/amount
        sort_order: 排序方向 - asc/desc
    """
    try:
        _ensure_connected()
        client = _get_client()
        code = _resolve_board_symbol(board_symbol)
        df = client.get_board_members(
            board_symbol=code, count=count,
            sort_type=_SORT_TYPE_MAP.get(sort_by, SortType.CHANGE_PCT),
            sort_order=_SORT_ORDER_MAP.get(sort_order, SortOrder.DESC),
        )
        return _ok(_df_to_records(df), board_symbol=board_symbol, resolved_code=code, sort_by=sort_by)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取资金流向数据")
def get_capital_flow(symbol: str) -> str:
    """获取股票资金流向（主力/散户/中单/大单）。

    Args:
        symbol: 股票代码
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        df = client.get_capital_flow(market=market, code=code)
        return _ok(_df_to_records(df), symbol=symbol)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取分时图数据")
def get_tick_chart(symbol: str, date: str = "", days: int = 1) -> str:
    """获取分时图数据，支持单日或多日。

    Args:
        symbol: 股票代码
        date: 日期 YYYYMMDD，为空则取最近交易日
        days: 取近几日（days>1时自动使用多日接口），默认1
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        date_int = int(date) if date else None
        if days > 1:
            df = client.get_tick_charts(market=market, code=code, date=date_int, days=days)
        else:
            df = client.get_tick_chart(market=market, code=code, date=date_int)
        return _ok(_df_to_records(df), symbol=symbol, date=date or "latest", days=days)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取成交明细")
def get_transactions(symbol: str, count: int = 100, date: str = "") -> str:
    """获取股票成交明细。

    Args:
        symbol: 股票代码
        count: 获取条数，默认100
        date: 日期 YYYYMMDD，为空取最新
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        date_int = int(date) if date else None
        df = client.get_transactions(market=market, code=code, count=count, date=date_int)
        return _ok(_df_to_records(df), symbol=symbol)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取股票基本信息")
def get_symbol_info(symbol: str) -> str:
    """获取股票基本信息（名称、开盘、收盘、成交量等）。

    Args:
        symbol: 股票代码
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        df = client.get_symbol_info(market=market, code=code)
        return _ok(_df_to_records(df), symbol=symbol)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取股票所属板块")
def get_belong_board(symbol: str) -> str:
    """获取股票所属的所有板块。

    Args:
        symbol: 股票代码
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        df = client.get_belong_board(market=market, code=code)
        return _ok(_df_to_records(df), symbol=symbol)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取集合竞价数据")
def get_auction(symbol: str) -> str:
    """获取股票集合竞价数据。

    Args:
        symbol: 股票代码
    """
    try:
        _ensure_connected()
        client = _get_client()
        market, code = _convert_symbol(symbol)
        df = client.get_auction(market=market, code=code)
        return _ok(_df_to_records(df), symbol=symbol)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取股票排行榜/涨跌列表")
def get_stock_quotes_list(category: str = "a", count: int = 80, sort_by: str = "change_pct", sort_order: str = "desc") -> str:
    """获取股票排行列表（涨幅榜/跌幅榜/量比榜等）。

    Args:
        category: 分类 - sh/sz/a/b/kcb/bj/cyb/etf/lof/hgt/sgt/board_hy/board_gn
        count: 获取数量，默认80
        sort_by: 排序字段 - change_pct/price/volume/amount/code
        sort_order: 排序方向 - asc/desc/none
    """
    try:
        _ensure_connected()
        client = _get_client()
        cat = _CATEGORY_MAP.get(category.lower(), Category.A)
        df = client.get_stock_quotes_list(
            category=cat, count=count,
            sort_type=_SORT_TYPE_MAP.get(sort_by, SortType.CHANGE_PCT),
            sort_order=_SORT_ORDER_MAP.get(sort_order, SortOrder.DESC),
        )
        return _ok(_df_to_records(df), category=category, sort_by=sort_by, sort_order=sort_order)
    except Exception as e:
        return _err(e)


@mcp.tool(description="获取异动股数据")
def get_unusual(market: str = "sh", count: int = 50) -> str:
    """获取市场异动股数据。

    Args:
        market: 市场 - sh(上海)/sz(深圳)
        count: 获取数量，默认50
    """
    try:
        _ensure_connected()
        client = _get_client()
        m = 1 if market.lower() in ("sh", "1") else 0
        df = client.get_unusual(market=m, count=count)
        return _ok(_df_to_records(df), market=market)
    except Exception as e:
        return _err(e)


# ──────────────────────── 启动入口 ────────────────────────

def _parse_args():
    import argparse
    parser = argparse.ArgumentParser(
        description="TDXQuant MCP Server - 通达信量化接口 MCP 服务器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python server.py                          # stdio 模式
  python server.py -t sse -p 8000           # SSE 模式
  python server.py --list                   # 列出所有工具
        """,
    )
    parser.add_argument("-t", "--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("-p", "--port", type=int, default=8000)
    parser.add_argument("-l", "--list", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.list:
        import asyncio
        async def _list():
            tools = await mcp.list_tools()
            print(f"=== TDXQuant MCP Server 共 {len(tools)} 个工具 ===\n")
            for t in tools:
                print(f"  {t.name}")
                if t.description:
                    print(f"    {t.description[:80]}")
                print()
        asyncio.run(_list())
        sys.exit(0)

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
