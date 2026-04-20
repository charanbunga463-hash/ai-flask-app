from flask import Flask, request, render_template, redirect, session, Response, stream_with_context
import sqlite3
import os
from dotenv import load_dotenv
from openai import OpenAI

# ---------------- ENV ----------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

# ---------------- DB ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "users.db")

def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# ---------------- AI ----------------
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

# ---------------- INIT DB ----------------
def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS conversations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS chats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        username TEXT,
        user_msg TEXT,
        bot_msg TEXT,
        type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

init_db()

# ---------------- HOME ----------------
@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")

    session.pop("conv_id", None)

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, title FROM conversations WHERE username=? ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()
    conn.close()

    return render_template("dashboard.html",
        user=session["user"],
        chat=[],
        conversations=conversations,
        active_chat=None
    )

# ---------------- STREAM ----------------
@app.route("/stream")
def stream():
    if "user" not in session:
        return "Unauthorized", 401

    user_input = request.args.get("input")

    def generate():
        conn = get_db()
        c = conn.cursor()

        conv_id = session.get("conv_id")

        if not conv_id:
            title = user_input[:30] if user_input else "New Chat"
            c.execute("INSERT INTO conversations (username, title) VALUES (?, ?)",
                      (session["user"], title))
            conv_id = c.lastrowid
            session["conv_id"] = conv_id
            conn.commit()

        full_text = ""

        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": "Reply in natural flowing paragraphs. Do not use bullet points or lists."
                    },
                    {"role": "user", "content": user_input}
                ],
                stream=True
            )

            for chunk in response:
                token = chunk.choices[0].delta.content or ""
                if token:
                    full_text += token
                    yield f"data: {token}\n\n"

        except Exception as e:
            yield f"data: Error: {str(e)}\n\n"

        # save raw text
        c.execute("""
            INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type)
            VALUES (?, ?, ?, ?, ?)
        """, (conv_id, session["user"], user_input, full_text, "text"))

        conn.commit()
        conn.close()

        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        conn = get_db()
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
            conn = get_db()
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
    app.run(debug=True, threaded=True)