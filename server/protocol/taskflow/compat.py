from datetime import datetime
from store import TZ
class timezone:
    @staticmethod
    def now():return datetime.now(TZ)
    @staticmethod
    def localdate():return datetime.now(TZ).date()
