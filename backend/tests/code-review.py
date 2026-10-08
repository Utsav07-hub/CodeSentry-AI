import sqlite3

def get_user(user_id):
    query = "SELECT * FROM users WHERE id=" + user_id
    connection = sqlite3.connect("users.db")
    return connection.execute(query).fetchone()