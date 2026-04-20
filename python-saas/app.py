from flask import Flask, request, render_template, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import markdown
from openai import OpenAI

# ---------------- ENV ----------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

# ---------------- AI ----------------
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

def query_ai(prompt):
    try:
        res = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Answer briefly."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=300
        )
        return res.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"

def generate_image(prompt):
    return f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}"

# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS conversations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS chats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        username TEXT,
        user_msg TEXT,
        bot_msg TEXT,
        type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()
    conn.close()

init_db()

# ---------------- HOME ----------------
@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")

    # 🔥 RESET CHAT → fresh page like ChatGPT
    session.pop("conv_id", None)

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    c.execute("SELECT id, title FROM conversations WHERE username=? ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        user=session["user"],
        chat=[],
        conversations=conversations,
        active_chat=None
    )

# ---------------- NEW CHAT ----------------
@app.route("/new_chat")
def new_chat():
    if "user" not in session:
        return redirect("/login")

    # 🔥 JUST RESET → do NOT create immediately
    session.pop("conv_id", None)

    return redirect("/")  # fresh empty screen

# ---------------- OPEN CHAT ----------------
@app.route("/chat/<int:conv_id>")
def open_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    # 🔥 CHECK EXISTS
    c.execute("SELECT id FROM conversations WHERE id=? AND username=?",
              (conv_id, session["user"]))
    if not c.fetchone():
        conn.close()
        return redirect("/")

    session["conv_id"] = conv_id

    # conversations
    c.execute("SELECT id, title FROM conversations WHERE username=? ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()

    # messages
    c.execute("SELECT id, user_msg, bot_msg, type FROM chats WHERE conversation_id=? ORDER BY id ASC",
              (conv_id,))
    rows = c.fetchall()

    conn.close()

    chat = []
    for r in rows:
        chat.append({
            "id": r[0],
            "type": r[3],
            "user": r[1],
            "bot": r[2] if r[3] == "text" else None,
            "image": r[2] if r[3] == "image" else None
        })

    return render_template(
        "dashboard.html",
        user=session["user"],
        chat=chat,
        conversations=conversations,
        active_chat=conv_id
    )

# ---------------- DELETE CHAT ----------------
@app.route("/delete_chat/<int:conv_id>", methods=["POST"])
def delete_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    c.execute("DELETE FROM chats WHERE conversation_id=?", (conv_id,))
    c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))

    conn.commit()

    # 🔥 GET ANOTHER CHAT
    c.execute("SELECT id FROM conversations WHERE username=? ORDER BY id DESC LIMIT 1",
              (session["user"],))
    next_chat = c.fetchone()

    conn.close()

    if next_chat:
        return redirect(f"/chat/{next_chat[0]}")
    else:
        session.pop("conv_id", None)
        return redirect("/")

# ---------------- TOOL ----------------
@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    user_input = request.form.get("input")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    conv_id = session.get("conv_id")

    # 🔥 CREATE CHAT ONLY WHEN USER TYPES
    if not conv_id:
        title = user_input[:30]

        c.execute("INSERT INTO conversations (username, title) VALUES (?, ?)",
                  (session["user"], title))

        conv_id = c.lastrowid
        session["conv_id"] = conv_id

    # IMAGE
    if any(k in user_input.lower() for k in ["image", "draw", "photo", "picture"]):
        img = generate_image(user_input)

        c.execute("INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type) VALUES (?, ?, ?, ?, ?)",
                  (conv_id, session["user"], user_input, img, "image"))

        conn.commit()
        conn.close()
        return redirect(f"/chat/{conv_id}")

    # TEXT
    result = query_ai(user_input)
    formatted = markdown.markdown(result)

    c.execute("INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type) VALUES (?, ?, ?, ?, ?)",
              (conv_id, session["user"], user_input, formatted, "text"))

    conn.commit()
    conn.close()

    return redirect(f"/chat/{conv_id}")

# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        conn = sqlite3.connect("users.db")
        c = conn.cursor()
        c.execute("SELECT password FROM users WHERE username=?", (user,))
        data = c.fetchone()
        conn.close()

        if data and pwd == data[0]:
            session["user"] = user
            session.pop("conv_id", None)
            return redirect("/")
        else:
            error = "Invalid credentials"

    return render_template("login.html", error=error)

# ---------------- REGISTER ----------------
@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        try:
            conn = sqlite3.connect("users.db")
            c = conn.cursor()
            c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (user, pwd))
            conn.commit()
            conn.close()
            return redirect("/login")
        except sqlite3.IntegrityError:
            error = "Username exists"

    return render_template("register.html", error=error)

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)