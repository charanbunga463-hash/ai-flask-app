from flask import Flask, request, render_template, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import markdown
from openai import OpenAI

# ---------------- LOAD ENV ----------------
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
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Answer only current question."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"

# ---------------- IMAGE ----------------
def generate_image(prompt):
    return f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}"

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    # USERS
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )''')

    # CONVERSATIONS
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # CHATS
    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        username TEXT,
        user_msg TEXT,
        bot_msg TEXT,
        type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()
    conn.close()

init_db()

# ---------------- HOME ----------------
@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    # Load conversations
    c.execute("SELECT id, title FROM conversations WHERE username=? ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()

    # Current conversation
    conv_id = session.get("conv_id")

    if not conv_id and conversations:
        conv_id = conversations[0][0]
        session["conv_id"] = conv_id

    messages = []
    if conv_id:
        c.execute("SELECT id, user_msg, bot_msg, type FROM chats WHERE conversation_id=? ORDER BY id ASC",
                  (conv_id,))
        data = c.fetchall()

        for row in data:
            if row[3] == "text":
                messages.append({"id": row[0], "type": "text", "user": row[1], "bot": row[2]})
            else:
                messages.append({"id": row[0], "type": "image", "user": row[1], "image": row[2]})

    conn.close()

    return render_template(
        "dashboard.html",
        user=session["user"],
        chat=messages,
        conversations=conversations,
        active_chat=conv_id
    )

# ---------------- NEW CHAT ----------------
@app.route("/new_chat")
def new_chat():
    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    c.execute("INSERT INTO conversations (username, title) VALUES (?, ?)",
              (session["user"], "New Chat"))

    session["conv_id"] = c.lastrowid

    conn.commit()
    conn.close()

    return redirect("/")

# ---------------- SWITCH CHAT ----------------
@app.route("/chat/<int:conv_id>")
def switch_chat(conv_id):
    session["conv_id"] = conv_id
    return redirect("/")

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
    conn.close()

    session.pop("conv_id", None)
    return redirect("/")

# ---------------- DELETE MESSAGE ----------------
@app.route("/delete_message/<int:msg_id>", methods=["POST"])
def delete_message(msg_id):
    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    c.execute("DELETE FROM chats WHERE id=? AND username=?", (msg_id, session["user"]))

    conn.commit()
    conn.close()

    return redirect("/")

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

# ---------------- TOOL ----------------
@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    user_input = request.form.get("input")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    conv_id = session.get("conv_id")

    # Create chat if none exists
    if not conv_id:
        c.execute("INSERT INTO conversations (username, title) VALUES (?, ?)",
                  (session["user"], user_input[:20]))
        conv_id = c.lastrowid
        session["conv_id"] = conv_id

    # IMAGE
    if any(k in user_input.lower() for k in ["image", "draw", "picture", "photo"]):
        image_url = generate_image(user_input)

        c.execute("INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type) VALUES (?, ?, ?, ?, ?)",
                  (conv_id, session["user"], user_input, image_url, "image"))

        conn.commit()
        conn.close()
        return redirect("/")

    # TEXT
    result = query_ai(user_input)
    formatted = markdown.markdown(result)

    c.execute("INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type) VALUES (?, ?, ?, ?, ?)",
              (conv_id, session["user"], user_input, formatted, "text"))

    conn.commit()
    conn.close()

    return redirect("/")

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)