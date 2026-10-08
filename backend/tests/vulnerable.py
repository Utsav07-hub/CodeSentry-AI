import sqlite3
# CodeSentry webhook test
# No-op update to trigger a pull_request synchronize delivery.
def get_user(user_id):
    query = "SELECT * FROM users WHERE id=" + user_id
    connection = sqlite3.connect("users.db")
    return connection.execute(query).fetchone()