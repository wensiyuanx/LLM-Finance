import lightgbm as lgb
import pandas as pd
import numpy as np
import os
import logging
from strategy.indicators import calculate_indicators

logger = logging.getLogger(__name__)

class MLPredictor:
    """
    LightGBM-based model to predict the probability of a positive return in the next N periods.
    """
    def __init__(self, model_dir="models"):
        self.model_dir = model_dir
        os.makedirs(model_dir, exist_ok=True)
        self.models = {}  # Cache loaded models per ticker/market

    def prepare_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Create predictive features from raw K-line data.
        """
        # Ensure base indicators are calculated
        if 'RSI_14' not in df.columns:
            df = calculate_indicators(df)
            
        features = pd.DataFrame(index=df.index)
        
        # 1. Momentum Features
        features['RSI'] = df['RSI_14']
        features['MACD'] = df.get('MACD', 0)
        features['ROC_10'] = df.get('ROC_10', df['close'].pct_change(10))
        features['ROC_20'] = df.get('ROC_20', df['close'].pct_change(20))
        
        # 2. Trend Features
        features['ADX'] = df.get('ADX_14', 0)
        features['Dist_SMA20'] = (df['close'] - df.get('SMA_20', df['close'])) / df.get('SMA_20', df['close'])
        features['Dist_SMA60'] = (df['close'] - df.get('SMA_60', df['close'])) / df.get('SMA_60', df['close'])
        
        # 3. Volatility Features
        features['ATR_Ratio'] = df.get('ATR_14', 0) / df['close']
        if 'BOLL_UPPER' in df.columns and 'BOLL_LOWER' in df.columns:
            features['BB_Width'] = (df['BOLL_UPPER'] - df['BOLL_LOWER']) / df['close']
            features['BB_Position'] = (df['close'] - df['BOLL_LOWER']) / (df['BOLL_UPPER'] - df['BOLL_LOWER'] + 1e-8)
        else:
            features['BB_Width'] = 0
            features['BB_Position'] = 0.5
            
        # 4. Volume Features
        features['Vol_Ratio'] = df['volume'] / df.get('VOL_SMA_5', df['volume'].rolling(5).mean() + 1e-8)
        
        return features.replace([np.inf, -np.inf], np.nan).fillna(0)

    def train_model(self, df: pd.DataFrame, code: str, forward_periods: int = 3):
        """
        Train a LightGBM model for a specific ticker.
        Target: 1 if return after `forward_periods` > 0, else 0.
        """
        if len(df) < 100:
            logger.warning(f"Not enough data to train ML model for {code} (need 100, got {len(df)})")
            return False
            
        # Calculate future return target
        df['Target_Return'] = df['close'].shift(-forward_periods) / df['close'] - 1
        df['Target'] = (df['Target_Return'] > 0.005).astype(int) # Target is 1 if it goes up > 0.5%
        
        # Drop the last N periods where target is NaN
        train_df = df.dropna(subset=['Target_Return']).copy()
        
        features = self.prepare_features(train_df)
        X = features.values
        y = train_df['Target'].values
        
        # Simple time-series split (last 20% for validation)
        split_idx = int(len(X) * 0.8)
        X_train, X_val = X[:split_idx], X[split_idx:]
        y_train, y_val = y[:split_idx], y[split_idx:]
        
        train_data = lgb.Dataset(X_train, label=y_train)
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)
        
        params = {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'learning_rate': 0.05,
            'num_leaves': 31,
            'max_depth': -1,
            'feature_fraction': 0.8,
            'verbose': -1
        }
        
        logger.info(f"Training LightGBM model for {code}...")
        model = lgb.train(
            params,
            train_data,
            num_boost_round=100,
            valid_sets=[val_data],
            callbacks=[lgb.early_stopping(stopping_rounds=10, verbose=False)]
        )
        
        model_path = os.path.join(self.model_dir, f"lgb_{code}.txt")
        model.save_model(model_path)
        self.models[code] = model
        logger.info(f"Successfully trained and saved ML model for {code}")
        return True

    def predict_prob(self, df: pd.DataFrame, code: str) -> float:
        """
        Predict the probability of a positive return for the latest row.
        """
        model_path = os.path.join(self.model_dir, f"lgb_{code}.txt")
        
        if code not in self.models:
            if os.path.exists(model_path):
                self.models[code] = lgb.Booster(model_file=model_path)
            else:
                # If no model exists, fallback to a neutral 0.5 probability
                return 0.5
                
        model = self.models[code]
        features = self.prepare_features(df)
        
        # Predict on the latest row
        latest_features = features.iloc[-1:].values
        prob = model.predict(latest_features)[0]
        
        return float(prob)

# Global singleton
ml_predictor = MLPredictor()
