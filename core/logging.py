import logging
import os
from datetime import datetime
import config

logger = logging.getLogger('trading_system')


def setup_logging():
    log_config = config.LOG_CONFIG
    
    level = getattr(logging, log_config['level'])
    logger.setLevel(level)
    
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    if log_config.get('console', True):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
    
    log_file = log_config.get('file')
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def log_signal(symbol: str, signal_data: dict):
    logger.info(f"SIGNAL: {symbol} | {signal_data.get('direction')} | {signal_data.get('structure')}")


def log_trade(trade_data: dict):
    pnl = trade_data.get('pnl', 0)
    status = "WIN" if pnl > 0 else "LOSS"
    
    logger.info(
        f"TRADE: {trade_data.get('symbol')} | {status} | "
        f"PnL: ₹{pnl:.2f} | {trade_data.get('reason')}"
    )


def log_error(symbol: str, error: str):
    logger.error(f"ERROR: {symbol} | {error}")


def log_market_data(symbol: str, data: dict):
    logger.debug(f"DATA: {symbol} | O: {data.get('open')} | H: {data.get('high')} | "
                 f"L: {data.get('low')} | C: {data.get('close')} | V: {data.get('volume')}")


logger = setup_logging()