import threading
import logging

logger = logging.getLogger(__name__)

class GlobalTradeLock:
    """
    A global reentrant lock to prevent concurrent trade executions
    between the scheduled analysis jobs and real-time monitoring.
    """
    _lock = threading.RLock()
    
    @classmethod
    def acquire(cls, timeout=30):
        """Acquire the trade lock with a timeout."""
        # logger.debug("Attempting to acquire Global Trade Lock...")
        success = cls._lock.acquire(timeout=timeout)
        if not success:
            logger.error(f"Failed to acquire Global Trade Lock after {timeout}s!")
        return success
        
    @classmethod
    def release(cls):
        """Release the trade lock."""
        # logger.debug("Releasing Global Trade Lock...")
        try:
            cls._lock.release()
        except RuntimeError:
            # Already released or not held by this thread
            pass

class TradeLockContext:
    """Context manager for the Global Trade Lock."""
    def __enter__(self):
        GlobalTradeLock.acquire()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        GlobalTradeLock.release()
