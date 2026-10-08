from app.services.ai_service import analyze_diff
from app.tasks import process_pull_request_task


payload = {
    "action": "opened",
    "pull_request": {
        "number": 1
    },
    "repository": {
        "full_name": "test/test-repo",
        "owner": {
            "id": 1,
            "login": "test"
        }
    },
    "sender": {
        "id": 1,
        "login": "test"
    }
}


result = process_pull_request_task.delay(payload)

print("Task queued successfully.")
print("Task ID:", result.id)

diff = """
diff --git a/app.py b/app.py
index 1234567..7654321 100644
--- a/app.py
+++ b/app.py
@@ -1,5 +1,10 @@
+import sqlite3
+
+password = "admin123"
+
 def get_user(user_id):
-    return users[user_id]
+    query = "SELECT * FROM users WHERE id=" + user_id
+    connection = sqlite3.connect("users.db")
+    return connection.execute(query).fetchone()
"""


print("Sending diff to CodeSentry AI...\n")

review = analyze_diff(diff)

print(review)