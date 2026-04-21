from flask import Flask, request, render_template, redirect, session, Response, stream_with_context
import sqlite3
import os
from dotenv import load_dotenv
import markdown
from openai import OpenAI

# ---------------- ENV ----------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "users.db")

# ---------------- AI ----------------
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

def generate_chat_title(message):
    try:
        res = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Generate short title (max 5 words)."},
                {"role": "user", "content": message}
            ],
            max_tokens=20
        )
        return res.choices[0].message.content.strip()
    except:
        return message[:30]

def generate_image(prompt):
    return f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}"

# ---------------- DB ----------------
def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db()
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
                           active_chat=None)

# ---------------- OPEN CHAT ----------------
@app.route("/chat/<int:conv_id>")
def open_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id FROM conversations WHERE id=? AND username=?",
              (conv_id, session["user"]))
    if not c.fetchone():
        return redirect("/")

    session["conv_id"] = conv_id

    c.execute("SELECT user_msg, bot_msg, type FROM chats WHERE conversation_id=? ORDER BY id",
              (conv_id,))
    rows = c.fetchall()

    c.execute("SELECT id, title FROM conversations WHERE username=? ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()

    conn.close()

    chat = []
    for r in rows:
        chat.append({
            "type": r[2],
            "user": r[0],
            "bot": r[1] if r[2] == "text" else None,
            "image": r[1] if r[2] == "image" else None
        })

    return render_template("dashboard.html",
                           user=session["user"],
                           chat=chat,
                           conversations=conversations,
                           active_chat=conv_id)

# ---------------- STREAM (🔥 MAIN FEATURE) ----------------
@app.route("/stream", methods=["POST"])
def stream():
    if "user" not in session:
        return "Unauthorized", 401

    user_input = request.form.get("input")

    conn = get_db()
    c = conn.cursor()

    conv_id = session.get("conv_id")

    # create chat if new
    if not conv_id:
        title = generate_chat_title(user_input)
        c.execute("INSERT INTO conversations (username, title) VALUES (?, ?)",
                  (session["user"], title))
        conv_id = c.lastrowid
        session["conv_id"] = conv_id
        conn.commit()

    # IMAGE (no streaming)
    if any(k in user_input.lower() for k in ["image", "draw", "photo", "picture"]):
        img = generate_image(user_input)

        c.execute("""INSERT INTO chats 
            (conversation_id, username, user_msg, bot_msg, type)
            VALUES (?, ?, ?, ?, ?)""",
                  (conv_id, session["user"], user_input, img, "image"))

        conn.commit()
        conn.close()
        return img

    # TEXT STREAM
    def generate():
        full_text = ""

        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": user_input}],
            stream=True
        )

        for chunk in response:
            if chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                full_text += text
                yield text

        # save after complete
        formatted = markdown.markdown(full_text)

        c.execute("""INSERT INTO chats 
            (conversation_id, username, user_msg, bot_msg, type)
            VALUES (?, ?, ?, ?, ?)""",
                  (conv_id, session["user"], user_input, formatted, "text"))

        conn.commit()
        conn.close()

    return Response(stream_with_context(generate()), content_type='text/plain')

# ---------------- DELETE ----------------
@app.route("/delete_chat/<int:conv_id>", methods=["POST"])
def delete_chat(conv_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("DELETE FROM chats WHERE conversation_id=?", (conv_id,))
    c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    conn.commit()

    c.execute("SELECT id FROM conversations WHERE username=? ORDER BY id DESC LIMIT 1",
              (session["user"],))
    next_chat = c.fetchone()

    conn.close()

    if next_chat:
        return redirect(f"/chat/{next_chat[0]}")
    else:
        session.pop("conv_id", None)
        return redirect("/")

# ---------------- EDIT ----------------
@app.route("/edit_chat/<int:conv_id>", methods=["POST"])
def edit_chat(conv_id):
    title = request.form.get("title")

    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE conversations SET title=? WHERE id=? AND username=?",
              (title, conv_id, session["user"]))
    conn.commit()
    conn.close()

    return redirect(f"/chat/{conv_id}")

# ---------------- AUTH ----------------
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
            session.pop("conv_id", None)
            return redirect("/")
        else:
            error = "Invalid credentials"

    return render_template("login.html", error=error)

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

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)