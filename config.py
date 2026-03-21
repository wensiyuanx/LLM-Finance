import os
import yaml
import logging

logger = logging.getLogger(__name__)

# Singleton instance
_config = None

def load_config(config_path="config.yaml", force=False):
    global _config
    if _config is not None and not force:
        return _config
        
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(base_dir, config_path)
        with open(full_path, "r", encoding="utf-8") as f:
            _config = yaml.safe_load(f)
        logger.info(f"Configuration loaded from {full_path}")
    except Exception as e:
        logger.error(f"Failed to load config from {config_path}: {e}")
        # Return fallback default configuration
        _config = {
            "global": {"data_window_days": 550, "incremental_fetch_overlap_days": 10},
            "strategies": {
                "leveraged_etf": {
                    "max_tranches": 3, "grid_drop_pct": 0.08, 
                    "atr_stop_mult": 3.0, "atr_trail_mult": 2.0,
                    "volume_surge_mult": 1.5, "adx_min_trend": 20, "adx_super_trend": 40,
                    "rsi_extreme_overbought": 85, "extreme_profit_taking": 0.15,
                    "initial_position_pct": 0.50
                },
                "broad_etf": {"max_tranches": 4, "grid_drop_pct": 0.05, "profit_target_pct": 0.05},
                "standard_stock": {"atr_stop_loss_mult": 2.5, "atr_take_profit_mult": 3.0, "fixed_stop_loss": -0.08, "fixed_take_profit": 0.15}
            }
        }
    return _config

def get_config():
    global _config
    if _config is None:
        return load_config()
    return _config

def refresh_config():
    """Hot-reload configuration from YAML and Database."""
    global _config
    from database.db import SessionLocal
    from database.models import ConfigParameter
    
    # 1. Start with fresh load from YAML
    new_config = load_config(force=True)
    
    # 2. Layer with Database overrides
    try:
        db = SessionLocal()
        try:
            params = db.query(ConfigParameter).all()
            for p in params:
                category = p.category
                key = p.key
                value = p.value
                
                # Type casting
                try:
                    if "." in value:
                        casted_value = float(value)
                    elif value.lower() in ("true", "false"):
                        casted_value = value.lower() == "true"
                    else:
                        casted_value = int(value)
                except ValueError:
                    casted_value = value
                
                if category in new_config["strategies"]:
                    new_config["strategies"][category][key] = casted_value
                elif category == "global":
                    new_config["global"][key] = casted_value
            logger.info(f"Configuration refreshed with {len(params)} database overrides.")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Failed to refresh config from database: {e}")
    
    _config = new_config
    return _config
