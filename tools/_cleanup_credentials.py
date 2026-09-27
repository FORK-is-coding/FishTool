import sqlite3
from pathlib import Path

path = Path(r"D:\tasks\cola\bili_ops_toolbox\data\bili_ops.db")
connection = sqlite3.connect(path)
try:
    connection.execute("UPDATE cookie_pool SET cookie_data = '', sessdata = '', bili_jct = '', buvid3 = ''")
    connection.execute("UPDATE accounts SET cookie_hash = NULL")
    connection.commit()
finally:
    connection.close()
print("database credentials cleared")
